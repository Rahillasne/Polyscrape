"""Tests for the LLM accelerator adapter (no network, no API key needed).

The Gemini call is monkeypatched to return a fixed payload, proving that
``extract`` parses structured output into our record shape with
``source_type='llm'``.
"""

from __future__ import annotations

from pipeline.sources.llm_accelerator import LlmAcceleratorAdapter

_FAKE_PAYLOAD = {
    "records": [
        {
            "name": "Antler Residency — Fall 2026",
            "category": "accelerator",
            "organizer": "Antler",
            "deadline": "2026-09-15",
            "rolling": False,
            "event_date": "2026-10-01",
            "location": "New York, NY",
            "funding": "$100k",
            "stage": "pre-seed",
            "apply_url": "https://www.antler.co/apply",
        },
        {
            "name": "Antler Rolling Cohort",
            "category": "accelerator",
            "organizer": "Antler",
            "deadline": "",
            "rolling": True,
            "location": "Remote",
            "funding": "equity-free",
            "stage": "any",
            "apply_url": "https://www.antler.co/apply",
        },
    ]
}


def _adapter() -> LlmAcceleratorAdapter:
    return LlmAcceleratorAdapter(
        url="https://www.antler.co/apply",
        organizer="Antler",
        name_hint="Antler Residency",
    )


def test_extract_parses_records(monkeypatch):
    adapter = _adapter()
    # Replace the Gemini call entirely — no network, no key.
    monkeypatch.setattr(adapter, "_call_gemini", lambda text: _FAKE_PAYLOAD)

    html = "<html><body><h1>Apply to Antler</h1><p>Deadline Sep 15</p></body></html>"
    records = adapter.extract(html)

    assert len(records) == 2
    for rec in records:
        assert rec["source_type"] == "llm"
        assert rec["source_url"] == "https://www.antler.co/apply"
        assert rec["organizer"] == "Antler"


def test_extract_normalizes_fields(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_call_gemini", lambda text: _FAKE_PAYLOAD)

    records = adapter.extract("<html><body>x</body></html>")
    first = records[0]
    # Funding normalized, deadline parsed to ISO 'Z', slug derived.
    assert first["funding"] == "$100,000"
    assert first["deadline"] == "2026-09-15T00:00:00Z"
    assert first["event_date"] == "2026-10-01"
    assert first["slug"] == "antler-residency-fall-2026-2026"


def test_rolling_record_passes_through(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_call_gemini", lambda text: _FAKE_PAYLOAD)

    records = adapter.extract("<html><body>x</body></html>")
    rolling = records[1]
    assert rolling["rolling"] is True
    assert rolling["deadline"] is None
    assert rolling["funding"] == "Equity-free"
    # Rolling means no deadline ambiguity flag.
    assert rolling["needs_review"] is False


def test_missing_deadline_non_rolling_sets_needs_review(monkeypatch):
    adapter = _adapter()
    payload = {
        "records": [
            {
                "name": "Mystery Program",
                "category": "accelerator",
                "organizer": "Antler",
                "deadline": "",  # missing
                "rolling": False,  # but not rolling -> ambiguous
                "apply_url": "https://www.antler.co/apply",
            }
        ]
    }
    monkeypatch.setattr(adapter, "_call_gemini", lambda text: payload)

    records = adapter.extract("<html><body>x</body></html>")
    assert len(records) == 1
    assert records[0]["needs_review"] is True


def test_empty_html_returns_empty():
    adapter = _adapter()
    assert adapter.extract("") == []


def test_no_api_key_returns_empty(monkeypatch):
    # Real _call_gemini path with no key set must return [] (no crash).
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    adapter = _adapter()
    records = adapter.extract("<html><body>some text here</body></html>")
    assert records == []


def test_gemini_error_returns_empty(monkeypatch):
    adapter = _adapter()

    def boom(text):
        raise RuntimeError("simulated API failure")

    monkeypatch.setattr(adapter, "_call_gemini", boom)
    # Must swallow the error and return [] (pipeline never crashes).
    assert adapter.extract("<html><body>text</body></html>") == []
