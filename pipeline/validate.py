"""The data-quality gate.

:func:`validate_record` enforces every rule from the spec and returns a
human-readable list of failures. The gate is strict by design: it rejects
junk so a broken scrape can never pollute the committed dataset.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import requests

from pipeline.http import shared_session
from pipeline.normalize import parse_deadline
from pipeline.schema import (
    CATEGORIES,
    REQUIRED_NONEMPTY,
    SOURCE_TYPES,
    STAGES,
)

# Per-run memoization of URL reachability. Many records share a host/url, and
# the runner checks records concurrently, so we cache results behind a lock to
# avoid re-probing the same URL (and to stay thread-safe).
_URL_CACHE: dict[str, bool] = {}
_URL_LOCK = threading.Lock()

# Statuses that mean "the host answered, the page is alive, it just doesn't want
# an automated HEAD/GET" — we treat these as reachable so a legitimately
# bot-blocked program page is NOT dropped from the dataset.
_ALIVE_BUT_BLOCKING = frozenset((401, 403, 405, 406, 429, 999))


def reset_url_cache() -> None:
    """Clear the per-run URL reachability cache (call at the start of a run)."""
    with _URL_LOCK:
        _URL_CACHE.clear()


def is_distant_past(iso: str, days: int = 30) -> bool:
    """Return True if ``iso`` parses to a moment more than ``days`` ago.

    Unparseable input is treated as *not* distant-past here (the deadline
    presence/parse rules in :func:`validate_record` handle bad values).
    """
    parsed = parse_deadline(iso)
    if parsed is None:
        return False
    dt = datetime.strptime(parsed, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return dt < cutoff


def check_url_ok(url: str, timeout: float = 12.0) -> bool:
    """Return True if ``url`` is https and the host is reachable/alive.

    Policy (tuned to never drop *good* data while still catching dead links):
      * not https            -> False
      * 2xx / 3xx            -> True (reachable)
      * 401/403/405/406/429  -> True (alive, just blocking automated probes)
      * 5xx                  -> True (transient server error; don't drop)
      * 404/410/other 4xx    -> False (genuinely dead)
      * connection/DNS/timeout error -> False (unreachable)

    Results are memoized per run (thread-safe) so shared URLs are probed once.
    Uses the pooled, retrying shared session.
    """
    if not isinstance(url, str) or not url.startswith("https://"):
        return False

    with _URL_LOCK:
        if url in _URL_CACHE:
            return _URL_CACHE[url]

    ok = _probe_url(url, timeout)

    with _URL_LOCK:
        _URL_CACHE[url] = ok
    return ok


def _probe_url(url: str, timeout: float) -> bool:
    """Do the actual HEAD/GET reachability probe (no caching)."""
    session = shared_session()
    try:
        resp = session.head(url, allow_redirects=True, timeout=timeout)
        # Some servers reject HEAD outright; retry those with a light GET.
        if resp.status_code in (400, 403, 405, 406, 501):
            resp = session.get(
                url, allow_redirects=True, timeout=timeout, stream=True
            )
        code = resp.status_code
        if 200 <= code < 400:
            return True
        if code in _ALIVE_BUT_BLOCKING or code >= 500:
            return True
        return False  # 404/410/other client errors -> dead link
    except requests.RequestException:
        return False
    except Exception:
        # Defensive: never let a URL check crash the pipeline.
        return False


def validate_record(rec: dict, *, check_urls: bool = True) -> tuple[bool, list[str]]:
    """Validate a single record against the full contract.

    Returns ``(ok, reasons)`` where ``reasons`` lists *every* failure found
    (not just the first), so logs are actionable.

    Rules enforced:
      * all of :data:`REQUIRED_NONEMPTY` present and non-empty
      * ``category`` in :data:`CATEGORIES`, ``stage`` in :data:`STAGES`,
        ``source_type`` in :data:`SOURCE_TYPES`
      * ``deadline`` parses to valid ISO **or** ``rolling`` is True
      * if not rolling: ``deadline`` present and not distant-past
      * ``apply_url`` and ``source_url`` are https and (if ``check_urls``)
        reachable
    """
    reasons: list[str] = []

    # 1) Required non-empty fields.
    for field in REQUIRED_NONEMPTY:
        value = rec.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            reasons.append(f"missing required field: {field}")

    # 2) Enum membership.
    if rec.get("category") not in CATEGORIES:
        reasons.append(
            f"invalid category: {rec.get('category')!r} "
            f"(allowed: {sorted(CATEGORIES)})"
        )
    if rec.get("stage") not in STAGES:
        reasons.append(
            f"invalid stage: {rec.get('stage')!r} (allowed: {sorted(STAGES)})"
        )
    if rec.get("source_type") not in SOURCE_TYPES:
        reasons.append(
            f"invalid source_type: {rec.get('source_type')!r} "
            f"(allowed: {sorted(SOURCE_TYPES)})"
        )

    # 3) Deadline / rolling logic.
    rolling = bool(rec.get("rolling"))
    raw_deadline = rec.get("deadline")
    parsed_deadline = parse_deadline(raw_deadline)

    if not rolling:
        if parsed_deadline is None:
            reasons.append(
                "deadline missing or unparseable and rolling is False"
            )
        elif is_distant_past(parsed_deadline):
            reasons.append(
                f"deadline is in the distant past: {parsed_deadline}"
            )
    else:
        # Rolling: a deadline is optional, but if present it must parse.
        if raw_deadline not in (None, "") and parsed_deadline is None:
            reasons.append(f"rolling record has unparseable deadline: {raw_deadline!r}")

    # 4) URL scheme + reachability.
    for field in ("apply_url", "source_url"):
        url = rec.get(field)
        if not isinstance(url, str) or not url.startswith("https://"):
            reasons.append(f"{field} is not https: {url!r}")
        elif check_urls and not check_url_ok(url):
            reasons.append(f"{field} unreachable (non-200/HEAD failed): {url}")

    return (len(reasons) == 0, reasons)
