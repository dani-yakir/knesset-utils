"""Thin client for the Knesset v4 OData service.

Retries on 5xx and transport errors (a transient 500 was observed mid-crawl
during pre-build validation, on an otherwise-healthy small table -- this is
not hypothetical), but not on 4xx, which are real request errors -- except
429 and 481, which are throttling. The Knesset edge answers sustained bursts
from GitHub-hosted runners with a non-standard 481 partway through a run
(the identical request succeeds moments later), so those get their own,
longer exponential backoff.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://knesset.gov.il/OdataV4/ParliamentInfo"
DEFAULT_TIMEOUT = 60.0
DEFAULT_RETRIES = 5
DEFAULT_BACKOFF = 1.5
DEFAULT_THROTTLE_BACKOFF = 5.0  # 5, 10, 20, 40s -> ~75s worst case per request at 5 attempts
THROTTLE_STATUSES = frozenset({429, 481})

logger = logging.getLogger(__name__)


class ODataClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        backoff: float = DEFAULT_BACKOFF,
        throttle_backoff: float = DEFAULT_THROTTLE_BACKOFF,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.retries = retries
        self.backoff = backoff
        self.throttle_backoff = throttle_backoff
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
            if attempt < self.retries - 1:
                if resp.status_code in THROTTLE_STATUSES:
                    delay = self.throttle_backoff * 2**attempt
                    logger.warning(
                        "http %d (throttled) on %s -- retry %d/%d in %.0fs; body: %r",
                        resp.status_code, resp.url, attempt + 1, self.retries - 1, delay, resp.text[:200],
                    )
                    last_err = f"http {resp.status_code}"
                    time.sleep(delay)
                    continue
                if resp.status_code >= 500:
                    last_err = f"http {resp.status_code}"
                    time.sleep(self.backoff * (attempt + 1))
                    continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"OData request failed after {self.retries} attempts: {last_err}")
