"""Thin client for the Knesset v4 OData service.

Retries on 5xx and transport errors (a transient 500 was observed mid-crawl
during pre-build validation, on an otherwise-healthy small table -- this is
not hypothetical), but not on 4xx, which are real request errors.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://knesset.gov.il/OdataV4/ParliamentInfo"
DEFAULT_TIMEOUT = 60.0
DEFAULT_RETRIES = 5
DEFAULT_BACKOFF = 1.5


class ODataClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        backoff: float = DEFAULT_BACKOFF,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.retries = retries
        self.backoff = backoff
        self._client = httpx.Client(timeout=timeout, headers={"Accept": "application/json"})

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ODataClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get_metadata_xml(self) -> bytes:
        resp = self._client.get(f"{self.base_url}/$metadata", headers={"Accept": "application/xml"})
        resp.raise_for_status()
        return resp.content

    def get_entities(
        self,
        entity_set: str,
        *,
        filter: str | None = None,
        orderby: str | None = None,
        top: int | None = None,
        count: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if filter:
            params["$filter"] = filter
        if orderby:
            params["$orderby"] = orderby
        if top is not None:
            params["$top"] = top
        if count:
            params["$count"] = "true"
        return self._get_json(f"{self.base_url}/{entity_set}", params=params)

    def get_count(self, entity_set: str) -> int:
        data = self.get_entities(entity_set, top=0, count=True)
        return data["@odata.count"]

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        last_err: Exception | str | None = None
        for attempt in range(self.retries):
            try:
                resp = self._client.get(url, params=params)
            except httpx.TransportError as exc:
                last_err = exc
                time.sleep(self.backoff * (attempt + 1))
                continue
            if resp.status_code >= 500 and attempt < self.retries - 1:
                last_err = f"http {resp.status_code}"
                time.sleep(self.backoff * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"OData request failed after {self.retries} attempts: {last_err}")
