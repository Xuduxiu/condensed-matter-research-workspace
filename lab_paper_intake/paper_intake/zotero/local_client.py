from __future__ import annotations

from typing import Any

import httpx

from .errors import ZoteroLocalError
from .mapper import normalized_title_for_zotero
from .schemas import ZoteroStatus

DISABLED_WRITE_MESSAGE = (
    "Zotero Local API write operations are disabled in v0.3. "
    "Use RIS package import instead."
)


class ZoteroLocalClient:
    def __init__(
        self,
        base_url: str = "http://localhost:23119/api",
        http_client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = http_client or httpx.Client(timeout=timeout)

    def health_check(self) -> ZoteroStatus:
        try:
            response = self._request(
                "GET",
                "/users/0/items",
                params={"limit": "1", "format": "json", "include": "data"},
            )
            if 200 <= response.status_code < 300:
                return ZoteroStatus(connected=True, base_url=self.base_url)
            return ZoteroStatus(
                connected=False,
                error_message=_friendly_http_error(response.status_code),
                base_url=self.base_url,
            )
        except httpx.HTTPError:
            return ZoteroStatus(
                connected=False,
                error_message=(
                    "未连接到 Zotero。请先打开 Zotero 桌面端，并确认已允许本机应用通信。"
                ),
                base_url=self.base_url,
            )
        except ZoteroLocalError as exc:
            return ZoteroStatus(
                connected=False,
                error_message=_sanitize_exception(exc),
                base_url=self.base_url,
            )

    def find_existing_item(self, doi: str | None, title: str | None) -> dict[str, Any] | None:
        if doi:
            existing = self._search_items(doi)
            for item in existing:
                data = _item_data(item)
                if str(data.get("DOI", "")).strip().lower() == doi.strip().lower():
                    return item
        if title:
            normalized = normalized_title_for_zotero(title)
            if normalized:
                for item in self._search_items(title):
                    data = _item_data(item)
                    if normalized_title_for_zotero(data.get("title")) == normalized:
                        return item
        return None

    def create_collection(self, _name: str) -> str:
        raise ZoteroLocalError(DISABLED_WRITE_MESSAGE)

    def create_item(self, *_args: object, **_kwargs: object) -> str:
        raise ZoteroLocalError(DISABLED_WRITE_MESSAGE)

    def add_item_to_collection(self, *_args: object, **_kwargs: object) -> None:
        raise ZoteroLocalError(DISABLED_WRITE_MESSAGE)

    def create_note(self, *_args: object, **_kwargs: object) -> str:
        raise ZoteroLocalError(DISABLED_WRITE_MESSAGE)

    def create_link_attachment(self, *_args: object, **_kwargs: object) -> str | None:
        raise ZoteroLocalError(DISABLED_WRITE_MESSAGE)

    def import_local_pdf_attachment(self, *_args: object, **_kwargs: object) -> str | None:
        raise ZoteroLocalError(DISABLED_WRITE_MESSAGE)

    def add_tags(self, *_args: object, **_kwargs: object) -> None:
        raise ZoteroLocalError(DISABLED_WRITE_MESSAGE)

    def _search_items(self, query: str) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            "/users/0/items",
            params={
                "q": query,
                "limit": "25",
                "format": "json",
                "include": "data",
            },
        )
        data = response.json()
        return data if isinstance(data, list) else []

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if method.upper() != "GET":
            raise ZoteroLocalError(DISABLED_WRITE_MESSAGE)
        headers = {
            "Zotero-API-Version": "3",
            "Accept": "application/json",
            **kwargs.pop("headers", {}),
        }
        response = self._client.request(
            method,
            f"{self.base_url}{path}",
            headers=headers,
            **kwargs,
        )
        if response.status_code >= 400:
            raise ZoteroLocalError(_friendly_http_error(response.status_code))
        return response


def import_papers_to_zotero(*_args: object, **_kwargs: object) -> None:
    raise ZoteroLocalError(DISABLED_WRITE_MESSAGE)


def _item_data(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data")
    return data if isinstance(data, dict) else item


def _friendly_http_error(status_code: int) -> str:
    return (
        f"Zotero Local API 返回 HTTP {status_code}。"
        "当前 v0.3 仅使用 Local API 做连接检测，不通过 /api 直接写入 Zotero；"
        "请使用 RIS 导入包在 Zotero 中手动导入。"
    )


def _sanitize_exception(exc: Exception) -> str:
    text = str(exc)
    for marker in ["DEEPSEEK_API_KEY", "Authorization", "Bearer "]:
        text = text.replace(marker, "[redacted]")
    return text[:500]