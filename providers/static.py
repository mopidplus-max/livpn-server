"""Провайдер static: узлы из nodes.json.

Форматы файла:
  ["vless://...", "vless://..."]
  [{"uri": "vless://...", "name": "🇳🇱 Нидерланды 1"}, ...]
  {"nodes": [...], "traffic": {"upload": 0, "download": 0, "total": 0}}
"""
from __future__ import annotations

import json
import logging

import config
from providers.base import Node, ProviderError
from xray import Traffic, uri_name

log = logging.getLogger("livpn.provider.static")


class StaticProvider:
    key = "static"

    def fetch(self, sub) -> tuple[list[Node], Traffic]:
        path = config.NODES_FILE
        if not path.exists():
            raise ProviderError(
                f"Файл {path.name} не найден. Скопируйте nodes.example.json "
                "в nodes.json или выберите другой PROVIDER."
            )

        try:
            payload = json.loads(path.read_text("utf-8"))
        except Exception as exc:
            raise ProviderError(f"Не удалось прочитать {path.name}: {exc}") from exc

        traffic = Traffic()
        if isinstance(payload, dict):
            raw_nodes = payload.get("nodes") or payload.get("items") or []
            raw_traffic = payload.get("traffic") or {}
            if isinstance(raw_traffic, dict):
                traffic = Traffic(
                    raw_traffic.get("upload", 0),
                    raw_traffic.get("download", 0),
                    raw_traffic.get("total", 0),
                )
        elif isinstance(payload, list):
            raw_nodes = payload
        else:
            raise ProviderError("nodes.json должен содержать список или объект")

        nodes: list[Node] = []
        for item in raw_nodes:
            if isinstance(item, str):
                uri = item.strip()
                name = uri_name(uri)
            elif isinstance(item, dict):
                uri = str(item.get("uri") or item.get("link") or "").strip()
                name = str(item.get("name") or "").strip()
            else:
                continue
            if not uri or "://" not in uri:
                continue
            nodes.append(Node(uri, name))

        if not nodes:
            raise ProviderError("В nodes.json нет ни одной рабочей ссылки")

        return nodes, traffic
