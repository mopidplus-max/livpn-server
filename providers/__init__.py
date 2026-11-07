"""Фабрика провайдеров."""
from __future__ import annotations

from providers.base import Provider, ProviderError

_INSTANCES: dict[str, Provider] = {}


def get_provider(key: str) -> Provider:
    key = (key or "").strip().lower()

    if key in _INSTANCES:
        return _INSTANCES[key]

    if key == "static":
        from providers.static import StaticProvider

        instance: Provider = StaticProvider()
    elif key == "upstream":
        from providers.upstream import UpstreamProvider

        instance = UpstreamProvider()
    elif key == "xui":
        from providers.xui import XuiProvider

        instance = XuiProvider()
    elif key == "remnawave":
        from providers.remnawave import RemnawaveProvider

        instance = RemnawaveProvider()
    elif key == "vlesshub":
        from providers.vlesshub import VlessHubProvider

        instance = VlessHubProvider()
    else:
        raise ProviderError(f"Неизвестный PROVIDER: {key!r}")

    _INSTANCES[key] = instance
    return instance


__all__ = ["get_provider", "Provider", "ProviderError"]
