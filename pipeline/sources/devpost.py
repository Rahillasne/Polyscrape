"""Devpost hackathons adapter (public JSON feed, source_type='api').

Pulls open/upcoming hackathons from the public Devpost feed and maps them to
``competition`` records (pitch competitions / hackathons). Network failure is
non-fatal: :meth:`fetch` returns ``{}`` so the pipeline keeps running.

Devpost events have real, dated submission windows, so we parse the *end* of
``submission_period_dates`` into a concrete deadline and SKIP any entry whose
deadline cannot be parsed or has already passed — rather than marking it
``rolling`` — which keeps the committed dataset clean.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from pipeline.http import make_session
from pipeline.normalize import compute_slug, normalize_funding, parse_deadline
from pipeline.sources.base import ApiAdapter

log = logging.getLogger(__name__)

# Public Devpost hackathons feed. status[] filters to currently-running and
# upcoming events; order_by=deadline returns them deadline-ascending.
_DEVPOST_URL = (
    "https://devpost.com/api/hackathons"
    "?status[]=open&status[]=upcoming&order_by=deadline"
)

# Splits "Jun 01 - Aug 15, 2026" on the LAST hyphen/en-dash/em-dash so we keep
# only the end-of-window portion (which carries the year).
_DATE_SPLIT_RE = re.compile(r"[-–—]")

# First run of money-like characters (digits, commas, decimals) inside the
# prize HTML, e.g. "<span>50,000</span> in prizes" -> "50,000".
_MONEY_DIGITS_RE = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?")


class DevpostAdapter(ApiAdapter):
    """Adapter for the public Devpost hackathons JSON feed."""

    name = "devpost"
    source_type = "api"

    def __init__(self, pages: int = 5) -> None:
        # How many feed pages to walk. The feed is paginated (~10/ page), so a
        # handful of pages turns a dozen hackathons into many dozens.
        self.pages = max(1, pages)

    def fetch(self) -> object:
        """Walk several feed pages, merge + dedup hackathons; ``{}`` on total failure.

        Returns the canonical ``{"hackathons": [...]}`` shape so :meth:`extract`
        is unchanged. Stops early on an empty page; a single page failing is
        non-fatal.
        """
        session = make_session()
        merged: dict[str, dict] = {}
        any_ok = False
        for page in range(1, self.pages + 1):
            try:
                resp = session.get(f"{_DEVPOST_URL}&page={page}")
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:  # noqa: BLE001 - one page failing is non-fatal
                log.warning("Devpost page %d failed (%s); skipping", page, exc)
                continue
            if not isinstance(data, dict):
                continue
            any_ok = True
            hackathons = data.get("hackathons")
            if not isinstance(hackathons, list) or not hackathons:
                break  # no more results
            for h in hackathons:
                if isinstance(h, dict):
                    key = str(h.get("url") or h.get("id") or id(h))
                    merged.setdefault(key, h)
        if not any_ok and not merged:
            log.warning("Devpost fetch failed on all pages; returning empty payload")
            return {}
        return {"hackathons": list(merged.values())}

    def extract(self, raw) -> list[dict]:
        """Map Devpost hackathons to partial competition records.

        Skips entries whose deadline can't be parsed or is already past, whose
        title is empty, or whose URL is not https. Derived fields:
        ``category='competition'``, ``stage='any'``, ``source_type='api'``,
        ``rolling=False``.
        """
        records: list[dict] = []

        if not isinstance(raw, dict):
            log.warning("Devpost payload was not a dict; got %s", type(raw).__name__)
            return records

        hackathons = raw.get("hackathons")
        if not isinstance(hackathons, list):
            return records

        now = datetime.now(timezone.utc)

        for item in hackathons:
            if not isinstance(item, dict):
                continue

            # --- URL (apply == source); must be https. ---
            url = item.get("url")
            if not isinstance(url, str) or not url.startswith("https://"):
                continue

            # --- Title / name. ---
            title = str(item.get("title") or "").strip()
            if not title:
                continue

            # --- Deadline: parse the END of the submission window. ---
            deadline = self._parse_end_deadline(item.get("submission_period_dates"))
            if not deadline:
                continue  # unparseable -> skip (do NOT mark rolling)
            if self._is_past(deadline, now):
                continue  # already closed -> skip

            # --- Funding from the prize HTML (digits only). ---
            funding = self._parse_funding(item.get("prize_amount")) or ""

            # --- Location: map Online/blank -> Remote. ---
            displayed = item.get("displayed_location")
            location_raw = ""
            if isinstance(displayed, dict):
                location_raw = str(displayed.get("location") or "").strip()
            location = location_raw or "Remote"
            if location.lower() == "online":
                location = "Remote"

            organizer = str(item.get("organization_name") or "").strip() or "Devpost"

            # Slug includes the deadline year for stability across cycles.
            year = deadline[:4]
            slug = compute_slug(title, year)

            # confidence: high only when deadline + funding + location all known.
            has_location = bool(location_raw) and location_raw.lower() != "online"
            confidence = "high" if (funding and has_location) else "medium"

            records.append(
                {
                    "slug": slug,
                    "name": title,
                    "category": "competition",
                    "organizer": organizer,
                    "deadline": deadline,
                    "rolling": False,
                    "event_date": None,
                    "location": location,
                    "funding": funding,
                    "stage": "any",
                    "apply_url": url,
                    "source_url": url,
                    "source_type": "api",
                    "confidence": confidence,
                    "needs_review": False,
                }
            )

        return records

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _parse_end_deadline(period: object) -> str | None:
        """Parse the END date out of a 'Jun 01 - Aug 15, 2026' style string."""
        if not isinstance(period, str) or not period.strip():
            return None
        # Take the substring after the LAST hyphen/en-dash/em-dash.
        end = _DATE_SPLIT_RE.split(period)[-1].strip()
        if not end:
            return None
        return parse_deadline(end)

    @staticmethod
    def _is_past(deadline_iso: str, now: datetime) -> bool:
        """Return True if an ISO8601-Z deadline is strictly before ``now``."""
        try:
            dt = datetime.strptime(deadline_iso, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except (ValueError, TypeError):
            return False
        return dt < now

    @staticmethod
    def _parse_funding(prize_html: object) -> str | None:
        """Strip digits out of the prize HTML and normalize them to money."""
        if not isinstance(prize_html, str):
            return None
        match = _MONEY_DIGITS_RE.search(prize_html)
        if not match:
            return None
        return normalize_funding(match.group(0))
