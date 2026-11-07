"""Интерфейс источника конфигов.

Провайдер — единственное место, откуда в сервис попадают реальные узлы.
Меняя PROVIDER в .env, вы меняете «начинку», не трогая бота и раздатчик.
"""
from __future__ import annotations

from typing import Protocol, Sequence

from xray import Traffic, uri_name


class Node:
    __slots__ = ("uri", "name")

    def __init__(self, uri: str, name: str = ""):
        self.uri = (uri or "").strip()
        self.name = (name or uri_name(self.uri)).strip()

    def __repr__(self) -> str:  # для логов
        return f"<Node {self.name!r}>"


class Provider(Protocol):
    key: str

    def fetch(self, sub) -> tuple[Sequence[Node], Traffic]:
        """Вернуть свежий список узлов и текущий трафик для подписки sub."""
        ...


class ProviderError(RuntimeError):
    pass


def dedupe_nodes(nodes: Sequence[Node], limit: int | None = None) -> list[Node]:
    """Убрать полные дубли ссылок, сохранив порядок."""
    seen: set[str] = set()
    result: list[Node] = []
    for node in nodes:
        if not node.uri or node.uri in seen:
            continue
        seen.add(node.uri)
        result.append(node)
        if limit and len(result) >= limit:
            break
    return result
