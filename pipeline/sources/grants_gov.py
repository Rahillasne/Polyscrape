"""Grants.gov Search2 API adapter (public API, source_type='api').

Queries the public Grants.gov Search2 endpoint for startup/tech/innovation
relevant funding opportunities and maps the hits to grant records. Network
failure is non-fatal: :meth:`fetch` returns ``{}`` so the pipeline keeps
running, and :meth:`extract` is defensive about missing/unexpected structure.
"""

from __future__ import annotations

import logging

from pipeline.http import make_session
from pipeline.normalize import compute_slug, normalize_funding, parse_deadline
from pipeline.sources.base import ApiAdapter

log = logging.getLogger(__name__)

# Public Grants.gov Search2 endpoint (JSON POST).
_SEARCH_URL = "https://api.grants.gov/v1/api/search2"

# Base for the human-facing opportunity detail page (used for apply/source URLs).
_DETAIL_BASE = "https://www.grants.gov/search-results-detail/"

# Award-ceiling-ish fields that occasionally appear on a hit; first hit wins.
_CEILING_FIELDS = ("awardCeiling", "estimatedFunding", "awardFloor")


class GrantsGovAdapter(ApiAdapter):
    """Adapter for the Grants.gov Search2 opportunities API."""

    name = "grants_gov"
    source_type = "api"

    def __init__(
        self,
        rows: int = 50,
        keyword: str = "small business innovation",
        opp_statuses: str = "posted|forecasted",
    ) -> None:
        self.rows = rows
        self.keyword = keyword
        self.opp_statuses = opp_statuses

    def fetch(self) -> object:
        """POST the Search2 query as JSON; return ``{}`` on any failure."""
        body = {
            "rows": self.rows,
            "keyword": self.keyword,
            "oppStatuses": self.opp_statuses,
        }
        try:
            session = make_session()
            resp = session.post(
                _SEARCH_URL,
                json=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                log.warning(
                    "Grants.gov payload was not an object; got %s",
                    type(data).__name__,
                )
                return {}
            return data
        except Exception as exc:  # noqa: BLE001 - never raise out of fetch
            log.warning(
                "Grants.gov fetch failed (%s); returning empty payload", exc
            )
            return {}

    def extract(self, raw) -> list[dict]:
        """Map Grants.gov opportunity hits to partial grant records.

        Hits live under ``raw["data"]["oppHits"]``. Skips hits lacking a usable
        future close date. Derived fields: ``category='grant'``,
        ``stage='any'``, ``source_type='api'``, ``confidence='high'``,
        ``rolling=False``.
        """
        records: list[dict] = []

        if not isinstance(raw, dict):
            log.warning(
                "Grants.gov payload was not a dict; got %s", type(raw).__name__
            )
            return records

        data = raw.get("data")
        if not isinstance(data, dict):
            return records

        hits = data.get("oppHits")
        if not isinstance(hits, list):
            return records

        for hit in hits:
            if not isinstance(hit, dict):
                continue

            close_raw = hit.get("closeDate")
            deadline = parse_deadline(close_raw)
            if not deadline:
                # No close date (or unparseable) -> skip.
                continue
            if _is_past(deadline):
                # Past / distant deadline -> skip (gate would reject anyway).
                continue

            title = (hit.get("title") or "").strip()
            if not title:
                continue

            opp_id = hit.get("id")
            if opp_id in (None, ""):
                continue
            detail_url = _DETAIL_BASE + str(opp_id)

            organizer = (
                hit.get("agency")
                or hit.get("agencyName")
                or hit.get("agencyCode")
                or "Grants.gov"
            )

            year = _year_of(close_raw)
            slug = compute_slug(title, year)

            funding = ""
            for field in _CEILING_FIELDS:
                value = hit.get(field)
                if value not in (None, "", 0, "0"):
                    funding = normalize_funding(str(value)) or ""
                    break

            rec = {
                "slug": slug,
                "name": title,
                "category": "grant",
                "organizer": str(organizer).strip(),
                "deadline": deadline,
                "rolling": False,
                "event_date": None,
                "location": "Remote",
                "funding": funding,
                "stage": "any",
                "apply_url": detail_url,
                "source_url": detail_url,
                "source_type": "api",
                "confidence": "high",
                "needs_review": False,
            }
            records.append(rec)

        return records


def _is_past(iso: str, days: int = 30) -> bool:
    """Return True if ``iso`` is more than ``days`` before now (distant past).

    Mirrors the gate's distant-past rule so we drop hits the validator would
    reject anyway, without importing the validator (keeps the adapter light).
    """
    from datetime import datetime, timedelta, timezone

    parsed = parse_deadline(iso)
    if parsed is None:
        return True
    dt = datetime.strptime(parsed, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return dt < cutoff


def _year_of(raw) -> str | None:
    """Best-effort 4-digit year from a close date, for slug uniqueness."""
    parsed = parse_deadline(raw)
    if parsed is None:
        return None
    return parsed[:4]
