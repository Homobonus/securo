"""Loyalty-account links: signing in, keeping the tokens, syncing receipts.

Credentials are one Fernet-encrypted JSON object on the row:
`{"tokens": {...}, "pending": {"code_verifier": ...}}`. `pending` exists
between "show me the sign-in link" and "here is what the browser
showed"; `tokens` survive a re-authorization that is never completed, so
asking for a new link does not break a working connection.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.services.crypto import decrypt, encrypt
from app.models.receipt import Receipt, RetailConnection
from app.providers.retail import get_retail_provider
from app.providers.retail.base import (
    RetailAuthError,
    RetailProvider,
    RetailProviderError,
    new_http_client,
)
from app.services import receipt_service
from app.services.receipt_service import ReceiptError

logger = logging.getLogger(__name__)

#: Detail calls per sync. A first sync of a long history is spread over
#: several runs instead of one request that outlives every timeout.
MAX_RECEIPTS_PER_SYNC = 60


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    matched: int = 0
    has_more: bool = False


def _read_credentials(connection: RetailConnection) -> dict:
    raw = decrypt(connection.credentials)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _write_credentials(connection: RetailConnection, value: dict) -> None:
    connection.credentials = encrypt(json.dumps(value))


def _provider(connection: RetailConnection) -> RetailProvider:
    provider = get_retail_provider(connection.provider)
    if provider is None:
        raise ReceiptError(f"Unknown provider {connection.provider}", 400)
    return provider


async def get_connection(
    session: AsyncSession, connection_id: uuid.UUID, workspace_id: uuid.UUID
) -> Optional[RetailConnection]:
    connection = await session.get(RetailConnection, connection_id)
    if connection is None or connection.workspace_id != workspace_id:
        return None
    return connection


async def list_connections(session: AsyncSession, workspace_id: uuid.UUID) -> list[RetailConnection]:
    result = await session.execute(
        select(RetailConnection)
        .where(RetailConnection.workspace_id == workspace_id)
        .order_by(RetailConnection.created_at)
    )
    return list(result.scalars().all())


async def create_connection(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    provider_name: str,
    display_name: Optional[str] = None,
) -> tuple[RetailConnection, str]:
    """A pending connection and the sign-in link that completes it."""
    provider = get_retail_provider(provider_name)
    if provider is None:
        raise ReceiptError(f"Unknown provider {provider_name}", 422)
    pending = provider.start_authorization()
    connection = RetailConnection(
        workspace_id=workspace_id,
        user_id=user_id,
        provider=provider.name,
        display_name=(display_name or provider.display_name)[:255],
        status="pending",
        settings={},
    )
    _write_credentials(connection, {"pending": pending.state})
    session.add(connection)
    await session.flush()
    return connection, pending.auth_url


async def restart_authorization(session: AsyncSession, connection: RetailConnection) -> str:
    pending = _provider(connection).start_authorization()
    credentials = _read_credentials(connection)
    credentials["pending"] = pending.state
    _write_credentials(connection, credentials)
    await session.flush()
    return pending.auth_url


async def authorize(
    session: AsyncSession,
    connection: RetailConnection,
    *,
    redirect_url: Optional[str] = None,
    refresh_token: Optional[str] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> None:
    """Finish signing in, from a pasted redirect or a refresh token."""
    provider = _provider(connection)
    credentials = _read_credentials(connection)
    async with new_http_client(transport) as http:
        try:
            if refresh_token:
                tokens = await provider.credentials_from_refresh_token(refresh_token, http)
            elif redirect_url:
                tokens = await provider.complete_authorization(
                    credentials.get("pending") or {}, redirect_url, http
                )
            else:
                raise ReceiptError("Provide the redirect address or a refresh token", 422)
        except RetailAuthError as exc:
            raise ReceiptError(f"The store rejected the sign-in ({exc})", 400) from exc
        except RetailProviderError as exc:
            raise ReceiptError(f"The store could not be reached ({exc})", 502) from exc
    _write_credentials(connection, {"tokens": tokens})
    connection.status = "active"
    connection.last_error = None
    await session.flush()


async def update_connection(session: AsyncSession, connection: RetailConnection, data: dict) -> None:
    if data.get("display_name"):
        connection.display_name = data["display_name"].strip()[:255]
    if "match_account_id" in data:
        settings = dict(connection.settings or {})
        account_id = data["match_account_id"]
        if account_id is None:
            settings.pop("match_account_id", None)
        else:
            settings["match_account_id"] = str(account_id)
        connection.settings = settings
    await session.flush()


async def sync_connection(
    session: AsyncSession,
    connection: RetailConnection,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    max_receipts: int = MAX_RECEIPTS_PER_SYNC,
) -> SyncResult:
    """Fetch receipts the workspace does not have yet, then pair them.

    Commits as it goes: a sync interrupted halfway keeps the receipts it
    already stored, and the next run carries on from the store's list.
    Status is recorded on the row whatever happens; a refused token is
    `reauth_required`, anything else `error` and retried later.
    """
    provider = _provider(connection)
    credentials = _read_credentials(connection)
    tokens = credentials.get("tokens")
    if not tokens:
        raise ReceiptError("Connection is not signed in", 409)

    known = set(
        (
            await session.execute(
                select(Receipt.external_id).where(
                    Receipt.workspace_id == connection.workspace_id,
                    Receipt.source == provider.name,
                )
            )
        ).scalars()
    )
    result = SyncResult()
    cache: dict[str, uuid.UUID] = {}
    client = None
    try:
        async with new_http_client(transport) as http:
            client = provider.client(tokens, http)
            summaries = await client.list_receipts(known_ids=known)
            missing = [s for s in summaries if s.external_id not in known]
            # Oldest first, so an interrupted backfill leaves no gap behind.
            missing.reverse()
            result.has_more = len(missing) > max_receipts
            for summary in missing[:max_receipts]:
                data = await client.fetch_receipt(summary)
                if data is None:
                    continue
                _, created = await receipt_service.ingest_receipt(
                    session,
                    connection.workspace_id,
                    provider.name,
                    data,
                    connection_id=connection.id,
                    product_cache=cache,
                )
                if created:
                    result.created += 1
                else:
                    result.updated += 1
                _write_credentials(connection, {**credentials, "tokens": client.credentials})
                await session.commit()
        connection.status = "active"
        connection.last_error = None
    except RetailAuthError as exc:
        connection.status = "reauth_required"
        connection.last_error = str(exc)[:500]
    except RetailProviderError as exc:
        logger.warning("Retail sync %s failed: %s", connection.id, exc)
        connection.status = "error"
        connection.last_error = str(exc)[:500]
    if client is not None and connection.status != "reauth_required":
        # Refresh tokens rotate: the one used to start this sync may
        # already be spent, even when the sync itself failed later.
        _write_credentials(connection, {**credentials, "tokens": client.credentials})
    connection.last_sync_at = datetime.now(timezone.utc)
    # Pair everything still unpaired, not just today's receipts: the bank
    # transaction for last week's shopping may only have arrived now.
    result.matched = await receipt_service.auto_match(session, connection.workspace_id)
    await session.commit()
    return result
