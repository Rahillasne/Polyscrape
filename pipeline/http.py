"""Shared HTTP layer for fast, polite, resilient scraping.

Everything that touches the network goes through here so the whole pipeline gets:

* connection pooling + keep-alive (one :class:`requests.Session`),
* automatic retries with exponential backoff on flaky/ratelimited responses
  (429/5xx) — this is what fixes SBIR's intermittent HTTP 429,
* a sane default timeout on every request (so one hung host can't stall a run),
* a small :func:`concurrent_map` helper to fan many fetches / URL checks out
  across a thread pool (I/O-bound work — threads are the right tool).
"""

from __future__ import annotations

import concurrent.futures
import logging
from typing import Callable, Iterable, TypeVar

import requests
from requests.adapters import HTTPAdapter

try:  # urllib3 ships with requests; this import path is stable across versions.
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover - very old urllib3 fallback
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

log = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")

# A realistic-but-honest UA: real browser string + a bot tag pointing at the repo
# so site owners can identify the crawler. Many hosts 403 obvious bot agents.
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "PolyscrapeBot/1.0 (+https://github.com/Rahillasne/Polyscrape)"
)
DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
DEFAULT_TIMEOUT = 15.0


class _TimeoutSession(requests.Session):
    """A Session that applies a default timeout to every request.

    ``requests`` has no session-level default timeout; without one a single
    unresponsive host can hang the whole run. Per-call ``timeout=`` still wins.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        super().__init__()
        self._timeout = timeout

    def request(self, method, url, **kwargs):  # type: ignore[override]
        kwargs.setdefault("timeout", self._timeout)
        return super().request(method, url, **kwargs)


def make_session(
    retries: int = 3,
    backoff: float = 0.5,
    timeout: float = DEFAULT_TIMEOUT,
    pool: int = 32,
) -> requests.Session:
    """Build a pooled, retrying :class:`requests.Session`.

    Retries (with exponential backoff and ``Retry-After`` support) fire on
    429/500/502/503/504 for idempotent methods, so transient rate limits and
    blips self-heal instead of dropping a source.
    """
    session = _TimeoutSession(timeout=timeout)
    session.headers.update(DEFAULT_HEADERS)

    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(("GET", "HEAD", "POST")),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=pool, pool_maxsize=pool)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# A lazily-created process-wide session for ad-hoc callers (e.g. URL checks).
_SHARED: requests.Session | None = None


def shared_session() -> requests.Session:
    """Return a lazily-created, reused module-level session."""
    global _SHARED
    if _SHARED is None:
        _SHARED = make_session()
    return _SHARED


def concurrent_map(
    fn: Callable[[T], R],
    items: Iterable[T],
    max_workers: int = 16,
    label: str = "task",
) -> list[R | None]:
    """Map ``fn`` over ``items`` across a thread pool, preserving input order.

    I/O-bound by design (network fetches, HEAD checks). An exception in any one
    task is logged and becomes ``None`` in the results — one bad item never
    sinks the batch. Returns ``[]`` for empty input.
    """
    items_list = list(items)
    if not items_list:
        return []

    workers = max(1, min(max_workers, len(items_list)))
    results: list[R | None] = [None] * len(items_list)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_index = {
            pool.submit(fn, item): i for i, item in enumerate(items_list)
        }
        for future in concurrent.futures.as_completed(future_to_index):
            index = future_to_index[future]
            try:
                results[index] = future.result()
            except Exception as exc:  # noqa: BLE001 - isolate per-item failures
                log.warning("%s[%d] failed: %s", label, index, exc)
                results[index] = None
    return results
