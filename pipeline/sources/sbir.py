"""SBIR.gov solicitations adapter (public API, source_type='api').

Pulls open SBIR/STTR solicitations from the public SBIR.gov API and maps them
to grant records. Network failure is non-fatal: :meth:`fetch` returns ``[]`` so
the pipeline keeps running.
"""

from __future__ import annotations

import logging

import requests

from pipeline.http import make_session
from pipeline.normalize import compute_slug, parse_deadline
from pipeline.sources.base import ApiAdapter

log = logging.getLogger(__name__)

# Public SBIR.gov solicitations endpoint (JSON). ``open=1`` limits to
# currently-open solicitations; ``rows`` caps the page size.
_SBIR_URL = "https://api.www.sbir.gov/public/api/solicitations"

# Module-level pooled, retrying session: SBIR's API intermittently answers 429,
# so we lean on make_session()'s exponential backoff + Retry-After handling
# instead of a bare requests.get (which would just fail on the first 429).
_SESSION = make_session()


class SbirAdapter(ApiAdapter):
    """Adapter for the SBIR.gov open-solicitations API."""

    name = "sbir"
    source_type = "api"

    def __init__(self, rows: int = 50) -> None:
        self.rows = rows

    def fetch(self) -> object:
        """GET open SBIR solicitations as JSON; return ``[]`` on any failure.

        Uses the shared retrying session so the API's intermittent HTTP 429
        ("TooManyRequestsError") self-heals via exponential backoff instead of
        dropping the source on the first throttle.
        """
        params = {"open": 1, "rows": self.rows, "format": "json"}
        try:
            resp = _SESSION.get(
                _SBIR_URL,
                params=params,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("SBIR fetch failed (%s); returning empty payload", exc)
            return []

    def extract(self, raw) -> list[dict]:
        """Map SBIR solicitation objects to partial grant records.

        Skips entries lacking a usable application URL or a close date.
        Derived fields: ``category='grant'``, ``stage='any'``,
        ``source_type='api'``, ``confidence='high'``, ``rolling=False``.
        """
        records: list[dict] = []

        # The live API was rate-limited ("TooManyRequestsError") at the time of
        # writing, so the exact response shape couldn't be observed. Accept both
        # a bare list and a common ``{"solicitations": [...]}`` style envelope,
        # and probe several candidate key names per field so the mapping is
        # resilient to the real (unconfirmed) schema.
        items = self._as_item_list(raw)
        if items is None:
            log.warning("SBIR payload was not a list; got %s", type(raw).__name__)
            return records

        for item in items:
            if not isinstance(item, dict):
                continue

            # Close/deadline date can appear under a few keys depending on the
            # solicitation type; take the first that parses.
            close_raw = (
                item.get("close_date")
                or item.get("solicitation_close_date")
                or item.get("application_due_date")
                or item.get("current_status_close_date")
            )
            deadline = parse_deadline(close_raw)
            if not deadline:
                continue

            # Prefer the explicit solicitation URL; fall back to the agency URL.
            apply_url = (
                item.get("sbir_solicitation_link")
                or item.get("solicitation_agency_url")
                or item.get("solicitation_link")
                or item.get("agency_url")
                or item.get("url")
            )
            if not apply_url or not str(apply_url).startswith("https://"):
                continue

            title = (
                item.get("solicitation_title")
                or item.get("title")
                or ""
            ).strip()
            if not title:
                continue

            organizer = (
                item.get("agency")
                or item.get("solicitation_agency")
                or item.get("program")
                or "SBIR"
            )

            year = str(
                item.get("solicitation_year")
                or item.get("program_year")
                or ""
            ).strip()
            slug = compute_slug(title, year or None)

            rec = {
                "slug": slug,
                "name": title,
                "category": "grant",
                "organizer": str(organizer).strip(),
                "deadline": deadline,
                "rolling": False,
                "event_date": None,
                "location": "Remote",
                "funding": "",
                "stage": "any",
                "apply_url": str(apply_url),
                "source_url": str(apply_url),
                "source_type": "api",
                "confidence": "high",
                "needs_review": False,
            }
            records.append(rec)

        return records

    @staticmethod
    def _as_item_list(raw) -> list | None:
        """Return the list of solicitation objects from a list or envelope.

        The API may answer with a bare ``[...]`` or wrap the rows in a dict
        under one of a few common keys. Returns ``None`` when ``raw`` is neither
        a list nor a recognizable envelope, so the caller can warn and bail.
        """
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in ("solicitations", "results", "data", "items"):
                value = raw.get(key)
                if isinstance(value, list):
                    return value
        return None
