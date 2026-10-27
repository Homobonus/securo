"""Loyalty-account integrations that read store e-receipts.

Unlike bank providers these need no operator configuration: the clients
are public mobile-app clients, so every provider is always available.
"""
from typing import Optional

from app.providers.retail.base import RetailProvider
from app.providers.retail.biedronka import BiedronkaProvider
from app.providers.retail.lidl_plus import LidlPlusProvider

_PROVIDERS: dict[str, RetailProvider] = {
    p.name: p for p in (LidlPlusProvider(), BiedronkaProvider())
}


def get_retail_provider(name: str) -> Optional[RetailProvider]:
    return _PROVIDERS.get(name)


def list_retail_providers() -> list[RetailProvider]:
    return list(_PROVIDERS.values())
