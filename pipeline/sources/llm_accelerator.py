"""LLM-backed accelerator-page adapter (source_type='llm').

Fetches an accelerator's apply/program page, strips it to text, and asks Gemini
Flash to return structured records via JSON-mode structured output (NO regex).
Designed to run *without* a key: if ``GEMINI_API_KEY`` is unset, or the SDK /
API errors, extraction logs and returns ``[]`` rather than crashing.
"""

from __future__ import annotations

import json
import logging
import os

import requests
from bs4 import BeautifulSoup

from pipeline.http import shared_session
from pipeline.normalize import compute_slug, normalize_funding, parse_deadline
from pipeline.schema import GEMINI_RESPONSE_SCHEMA
from pipeline.sources.base import LlmAdapter

log = logging.getLogger(__name__)

_GEMINI_MODEL = "gemini-2.5-flash"

# Fields we hope the LLM returns; used to score confidence.
_SCORED_FIELDS = (
    "name",
    "category",
    "organizer",
    "deadline",
    "event_date",
    "location",
    "funding",
    "stage",
    "apply_url",
)

class LlmAcceleratorAdapter(LlmAdapter):
    """Extract accelerator funding records from a page using Gemini Flash."""

    source_type = "llm"

    def __init__(self, url: str, organizer: str, name_hint: str) -> None:
        self.url = url
        self.organizer = organizer
        self.name_hint = name_hint
        # A short, stable adapter id derived from the organizer/hint.
        self.name = compute_slug(name_hint or organizer or "accelerator")

    def cache_key(self) -> str:
        """Cache on the URL so different targets don't collide."""
        return f"{self.name}:{self.url}"

    def fetch(self) -> str:
        """GET the accelerator page HTML; return ``""`` on any failure.

        Routes through the shared pooled+retrying session (browser-like UA,
        backoff on 429/5xx) so transient blips don't drop the page.
        """
        try:
            resp = shared_session().get(self.url, headers={"Accept": "text/html"})
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            log.warning("LLM adapter fetch failed for %s (%s)", self.url, exc)
            return ""

    # -- Gemini call (isolated so it can be monkeypatched in tests) ----------

    def _call_gemini(self, text: str) -> dict:
        """Call Gemini Flash with structured output; return the parsed dict.

        Returns ``{"records": [...]}`` on success. Any missing key, import
        error, or API error is caught by the caller (:meth:`extract`).
        """
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            log.info(
                "GEMINI_API_KEY not set; skipping LLM extraction for %s", self.url
            )
            return {}

        # Import lazily so the package imports fine without the SDK installed.
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)

        prompt = (
            "You extract startup funding opportunities (accelerators, pitch "
            "competitions, grants, fellowships) from web page text. Return ONLY "
            "the structured data requested. For each distinct funding "
            "opportunity on the page, emit one record. Use category one of "
            "accelerator/competition/grant/fellowship and stage one of "
            "pre-seed/seed/any. If a deadline is rolling or not stated, set "
            "rolling=true and leave deadline empty. Do not invent values.\n\n"
            f"Organizer hint: {self.organizer}\n"
            f"Program hint: {self.name_hint}\n\n"
            "PAGE TEXT:\n"
            f"{text[:30000]}"
        )

        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=GEMINI_RESPONSE_SCHEMA,
        )
        resp = client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=prompt,
            config=config,
        )
        return json.loads(resp.text)

    def extract(self, html: str) -> list[dict]:
        """Strip HTML to text, call Gemini, and build record dicts.

        Never raises: SDK import errors, API errors, and bad JSON all log and
        yield ``[]``.
        """
        if not html:
            return []

        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        if not text.strip():
            return []

        try:
            payload = self._call_gemini(text)
        except ImportError as exc:
            log.warning("google-genai not importable (%s); returning []", exc)
            return []
        except Exception as exc:  # noqa: BLE001 - never crash the pipeline
            log.warning("Gemini call failed for %s (%s); returning []", self.url, exc)
            return []

        raw_records = []
        if isinstance(payload, dict):
            raw_records = payload.get("records") or []
        if not isinstance(raw_records, list):
            log.warning("Gemini returned non-list 'records' for %s", self.url)
            return []

        out: list[dict] = []
        for raw in raw_records:
            if not isinstance(raw, dict):
                continue

            name = (raw.get("name") or self.name_hint or "").strip()
            if not name:
                continue

            funding = normalize_funding(raw.get("funding"))
            deadline = parse_deadline(raw.get("deadline"))
            rolling = bool(raw.get("rolling"))

            # If the deadline was missing/ambiguous and it's not flagged
            # rolling, mark for human review.
            needs_review = False
            if deadline is None and not rolling:
                needs_review = True

            cycle = ""
            if deadline:
                cycle = deadline[:4]  # year, for slug stability
            slug = compute_slug(name, cycle or None)

            rec = {
                "slug": slug,
                "name": name,
                "category": (raw.get("category") or "accelerator").strip(),
                "organizer": (raw.get("organizer") or self.organizer or "").strip(),
                "deadline": deadline,
                "rolling": rolling,
                "event_date": parse_deadline(raw.get("event_date"))
                and parse_deadline(raw.get("event_date"))[:10]
                or None,
                "location": (raw.get("location") or "").strip(),
                "funding": funding or "",
                "stage": (raw.get("stage") or "any").strip(),
                "apply_url": (raw.get("apply_url") or self.url).strip(),
                "source_url": self.url,
                "source_type": "llm",
                "confidence": self._score_confidence(raw),
                "needs_review": needs_review,
            }
            out.append(rec)

        return out

    @staticmethod
    def _score_confidence(raw: dict) -> str:
        """Confidence from how many expected fields came back non-empty."""
        filled = 0
        for field in _SCORED_FIELDS:
            value = raw.get(field)
            if isinstance(value, str) and value.strip():
                filled += 1
            elif isinstance(value, bool):
                filled += 1
            elif value not in (None, ""):
                filled += 1
        ratio = filled / len(_SCORED_FIELDS)
        if ratio >= 0.75:
            return "high"
        if ratio >= 0.5:
            return "medium"
        return "low"
