"""Receipt photos: upload, read by a vision model, review, accept.

Gated by the `receipts` module, and inert until the deploy configures a
model (`OPENAI_API_KEY`): uploading then answers 409 and the app hides
the button.
"""
import uuid
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.receipts import _load_receipt, _receipt_detail, read_ctx, write_ctx
from app.core.database import get_async_session
from app.core.workspace_context import WorkspaceContext
from app.models.receipt import Receipt, ReceiptScan
from app.providers import get_storage_provider
from app.providers.receipt_scan import ReceiptScanError, scan_settings
from app.schemas.receipt import (
    ReceiptDetail,
    ReceiptScanAccept,
    ReceiptScanRead,
    ReceiptScanStatus,
    ReceiptScanStatusRead,
)
from app.services import receipt_scan_service as scans

router = APIRouter(prefix="/api/receipt-scans", tags=["receipts"])

#: A phone photo before the app shrinks it. The browser shrinks it first,
#: so this only stops a mistake (a video, a RAW file) from being read in.
MAX_UPLOAD_BYTES = 15 * 1024 * 1024


def _read(scan: ReceiptScan) -> ReceiptScanRead:
    draft = scans.draft_of(scan)
    return ReceiptScanRead(
        id=scan.id,
        status=cast(ReceiptScanStatus, scan.status),
        draft=draft,
        items_sum=scans.items_sum(draft.items) if draft else None,
        error=scan.error,
        model=scan.model,
        receipt_id=scan.receipt_id,
        created_at=scan.created_at,
    )


async def _load(session: AsyncSession, scan_id: uuid.UUID, workspace_id: uuid.UUID) -> ReceiptScan:
    scan = await session.scalar(
        select(ReceiptScan).where(ReceiptScan.id == scan_id, ReceiptScan.workspace_id == workspace_id)
    )
    if scan is None:
        raise HTTPException(status_code=404, detail="Receipt scan not found")
    return scan


def _require_enabled() -> None:
    if not scan_settings().enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="not_configured")


@router.get("/status", response_model=ReceiptScanStatusRead)
async def scan_status(ctx: WorkspaceContext = Depends(read_ctx)):
    config = scan_settings()
    return ReceiptScanStatusRead(enabled=config.enabled, model=config.model if config.enabled else None)


@router.get("", response_model=list[ReceiptScanRead])
async def list_scans(
    ctx: WorkspaceContext = Depends(read_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    """Scans still waiting for a person: read, failed, or being read."""
    rows = (
        await session.execute(
            select(ReceiptScan)
            .where(ReceiptScan.workspace_id == ctx.workspace.id, ReceiptScan.status != "accepted")
            .order_by(ReceiptScan.created_at.desc())
        )
    ).scalars().all()
    return [_read(scan) for scan in rows]


@router.post("", response_model=ReceiptScanRead, status_code=status.HTTP_201_CREATED)
async def upload_scan(
    file: UploadFile,
    ctx: WorkspaceContext = Depends(write_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    """Store the photo and read it. Answers once the model has (up to a minute or two)."""
    _require_enabled()
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="too_large")
    try:
        scan = await scans.create_scan(session, ctx.workspace.id, ctx.user_id, data)
    except ReceiptScanError as exc:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=exc.code)
    # The photo is safe before the slow part: a timeout leaves a scan to retry.
    await session.commit()
    await scans.read_scan(session, scan)
    await session.commit()
    return _read(scan)


@router.get("/{scan_id}", response_model=ReceiptScanRead)
async def get_scan(
    scan_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(read_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    return _read(await _load(session, scan_id, ctx.workspace.id))


@router.get("/{scan_id}/image")
async def scan_image(
    scan_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(read_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    scan = await _load(session, scan_id, ctx.workspace.id)
    try:
        data = await get_storage_provider().download(scan.storage_key)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Receipt photo not found")
    return Response(
        content=data,
        media_type=scan.content_type,
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.post("/{scan_id}/retry", response_model=ReceiptScanRead)
async def retry_scan(
    scan_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(write_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    scan = await _load(session, scan_id, ctx.workspace.id)
    _require_enabled()
    if scan.status == "accepted":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="already_accepted")
    scan.status = "processing"
    await session.commit()
    await scans.read_scan(session, scan)
    await session.commit()
    return _read(scan)


@router.post("/{scan_id}/accept", response_model=ReceiptDetail)
async def accept_scan(
    scan_id: uuid.UUID,
    payload: ReceiptScanAccept,
    ctx: WorkspaceContext = Depends(write_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    """Save the corrected draft as a receipt. Accepting again rewrites that receipt."""
    scan = await _load(session, scan_id, ctx.workspace.id)
    if scan.status == "processing":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="still_processing")
    receipt = await scans.accept_scan(session, scan, payload)
    receipt_id = receipt.id
    await session.commit()
    return _receipt_detail(await _load_receipt(session, receipt_id, ctx.workspace.id))


@router.delete("/{scan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scan(
    scan_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(write_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    """Discard the photo, and the receipt it became if it was accepted."""
    scan = await _load(session, scan_id, ctx.workspace.id)
    if scan.receipt_id is not None:
        receipt = await session.get(Receipt, scan.receipt_id)
        if receipt is not None:
            await session.delete(receipt)
    await scans.forget_image(scan)
    await session.delete(scan)
    await session.commit()
