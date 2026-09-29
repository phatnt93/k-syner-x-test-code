"""HTTP client for the inventory service's Products API (Vietful-shaped; the emulator locally).

Timeouts and retries follow D8 / requirements section 9.3: connect / read timeouts from `sync_config`, a
bounded number of attempts per request with exponential backoff + jitter. Only failures that can pass by
themselves are retried (connection errors, timeouts, 429, 5xx); a 4xx is a bug or a configuration problem and
fails at once.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]


class InventoryError(Exception):
    """The inventory service could not answer (after retries, or with a non-retryable error)."""


@dataclass(frozen=True)
class ClientConfig:
    base_url: str
    token: str
    connect_timeout_s: float
    read_timeout_s: float
    max_attempts: int
    backoff_base_s: float = 0.5
    backoff_max_s: float = 8.0


class InventoryClient:
    def __init__(
        self,
        config: ClientConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._config = config
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._http = httpx.AsyncClient(
            base_url=config.base_url,
            headers={"Authorization": f"Bearer {config.token}"},
            timeout=httpx.Timeout(config.read_timeout_s, connect=config.connect_timeout_s),
            transport=transport,
        )

    async def __aenter__(self) -> "InventoryClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._http.aclose()

    async def list_products(self, page_index: int, page_size: int) -> list[dict[str, Any]]:
        """`GET /api/v1/Products` — one page, ordered by productId. Raises `InventoryError`."""
        body = await self._get_json("/api/v1/Products", {"PageIndex": page_index, "PageSize": page_size})
        if not isinstance(body, list) or not all(isinstance(item, dict) for item in body):
            raise InventoryError("Products response is not a JSON array of objects")
        return body

    async def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        attempts = self._config.max_attempts
        for attempt in range(1, attempts + 1):
            try:
                resp = await self._http.get(path, params=params)
            except httpx.TransportError as exc:  # connect error, timeout, dropped connection
                problem = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            else:
                if resp.status_code < 400:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise InventoryError(f"GET {path}: response is not JSON") from exc
                problem = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if not _retryable(resp.status_code):
                    raise InventoryError(f"GET {path} failed: {problem}")

            if attempt == attempts:
                raise InventoryError(f"GET {path} failed after {attempts} attempts: {problem}")
            delay = self._backoff(attempt)
            log.warning(
                "inventory request failed, retrying in %.2fs (%d/%d): %s", delay, attempt, attempts, problem
            )
            await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _backoff(self, attempt: int) -> float:
        base = min(self._config.backoff_max_s, self._config.backoff_base_s * 2.0 ** (attempt - 1))
        return base + self._rng.uniform(0, base / 2)  # jitter spreads retries of concurrent clients


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500
