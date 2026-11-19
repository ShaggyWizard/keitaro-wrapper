"""Тонкий клиент Keitaro Admin API v1 (заголовок Api-Key)."""
from functools import lru_cache
from typing import Any

import httpx

from app.config import get_settings


class KeitaroError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Keitaro {status}: {message}")
        self.status = status
        self.message = message


class KeitaroClient:
    def __init__(self, base_url: str, api_key: str, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/admin_api/v1",
            headers={"Api-Key": api_key, "Accept": "application/json"},
            timeout=20,
            transport=transport,
        )

    def _request(self, method: str, path: str, **kwargs) -> Any:
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.TimeoutException as e:
            raise KeitaroError(504, "Keitaro не ответил вовремя") from e
        except httpx.HTTPError as e:
            raise KeitaroError(502, f"Не удалось связаться с Keitaro: {type(e).__name__}") from e
        if resp.status_code >= 400:
            try:
                body = resp.json()
                message = (body.get("error") or body.get("message")) if isinstance(body, dict) else None
                message = message or resp.text
            except ValueError:
                message = resp.text
            raise KeitaroError(resp.status_code, str(message)[:500])
        if not resp.content:
            return None
        return resp.json()

    # --- справочники
    def offers(self) -> list[dict]:
        return self._request("GET", "/offers")

    def domains(self) -> list[dict]:
        return self._request("GET", "/domains")

    def groups(self) -> list[dict]:
        return self._request("GET", "/groups", params={"type": "campaigns"})

    def traffic_sources(self) -> list[dict]:
        return self._request("GET", "/traffic_sources")

    # --- кампании и потоки
    def campaigns(self) -> list[dict]:
        result, offset, page = [], 0, 500
        while True:
            chunk = self._request("GET", "/campaigns", params={"limit": page, "offset": offset})
            result += chunk
            if len(chunk) < page:
                return result
            offset += page

    def campaign(self, campaign_id: int) -> dict:
        return self._request("GET", f"/campaigns/{campaign_id}")

    def create_campaign(self, payload: dict) -> dict:
        return self._request("POST", "/campaigns", json=payload)

    def delete_campaign(self, campaign_id: int) -> None:
        self._request("DELETE", f"/campaigns/{campaign_id}")

    def streams(self, campaign_id: int) -> list[dict]:
        return self._request("GET", f"/campaigns/{campaign_id}/streams")

    def create_stream(self, payload: dict) -> dict:
        return self._request("POST", "/streams", json=payload)

    def update_stream(self, stream_id: int, payload: dict) -> dict:
        return self._request("PUT", f"/streams/{stream_id}", json=payload)


    def close(self) -> None:
        self._http.close()


@lru_cache
def get_keitaro() -> KeitaroClient:
    """Один клиент (и пул соединений) на всё приложение."""
    s = get_settings()
    if not s.keitaro_api_key or not s.keitaro_url:
        raise KeitaroError(500, "Не заданы KEITARO_URL и KEITARO_API_KEY в .env")
    return KeitaroClient(s.keitaro_url, s.keitaro_api_key)
