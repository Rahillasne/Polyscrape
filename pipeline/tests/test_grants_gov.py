"""Tests for the Grants.gov adapter (offline — fetch is monkeypatched).

Proves that ``extract`` keeps only opportunities with a usable future close
date, maps them into our record shape, and degrades gracefully on a malformed
or empty payload. No network is touched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pipeline.sources.grants_gov import GrantsGovAdapter

# Dates relative to "now" so the test does not rot: one clearly future, one
# clearly in the distant past (well past the 30-day gate cutoff).
_FUTURE = (datetime.now(timezone.utc) + timedelta(days=90)).strftime("%m/%d/%Y")
_PAST = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%m/%d/%Y")
_FUTURE_YEAR = (datetime.now(timezone.utc) + timedelta(days=90)).strftime("%Y")


def _payload() -> dict:
    """Realistic Search2 response: one valid, one missing date, one past."""
    return {
        "errorcode": 0,
        "data": {
            "searchParams": {"keyword": "small business innovation"},
            "hitCount": 3,
            "oppHits": [
                {
                    "id": "350123",
                    "number": "RFA-OD-26-001",
                    "title": "Small Business Innovation Research Phase I",
                    "agencyCode": "HHS-NIH11",
                    "agency": "National Institutes of Health",
                    "agencyName": "National Institutes of Health",
                    "openDate": _PAST,
                    "closeDate": _FUTURE,
                    "oppStatus": "posted",
                    "docType": "synopsis",
                    "awardCeiling": "500000",
                },
                {
                    "id": "350124",
                    "number": "RFA-OD-26-002",
                    "title": "Rolling Tech Transfer Opportunity",
                    "agencyCode": "NSF",
                    "agency": "National Science Foundation",
                    "openDate": _PAST,
                    "closeDate": "",  # no close date -> must be skipped
                    "oppStatus": "forecasted",
                    "docType": "forecast",
                },
                {
                    "id": "350125",
                    "number": "RFA-OD-25-009",
                    "title": "Expired Innovation Grant",
                    "agencyCode": "DOE",
                    "agency": "Department of Energy",
                    "openDate": _PAST,
                    "closeDate": _PAST,  # distant past -> must be skipped
                    "oppStatus": "posted",
                    "docType": "synopsis",
                },
            ],
        },
    }


def test_extract_keeps_only_valid_future(monkeypatch):
    adapter = GrantsGovAdapter()
    monkeypatch.setattr(adapter, "fetch", lambda: _payload())

    records = adapter.extract(adapter.fetch())

    # Only the single future-dated hit survives.
    assert len(records) == 1
    rec = records[0]

    assert rec["category"] == "grant"
    assert rec["stage"] == "any"
    assert rec["source_type"] == "api"
    assert rec["confidence"] == "high"
    assert rec["rolling"] is False
    assert rec["name"] == "Small Business Innovation Research Phase I"
    assert rec["organizer"] == "National Institutes of Health"
    assert rec["location"] == "Remote"

    # Detail URL is the https grants.gov page for both apply + source.
    expected_url = "https://www.grants.gov/search-results-detail/350123"
    assert rec["apply_url"] == expected_url
    assert rec["source_url"] == expected_url
    assert rec["apply_url"].startswith("https://")

    # Deadline parsed to ISO 8601 UTC with trailing Z; slug derived + set.
    assert rec["deadline"] is not None
    assert rec["deadline"].endswith("Z")
    assert "T" in rec["deadline"]
    assert rec["slug"]
    assert rec["slug"] == (
        "small-business-innovation-research-phase-i-" + _FUTURE_YEAR
    )

    # Award ceiling surfaced and normalized.
    assert rec["funding"] == "$500,000"


def test_empty_payload_yields_empty():
    adapter = GrantsGovAdapter()
    assert adapter.extract({}) == []


def test_malformed_payloads_yield_empty():
    adapter = GrantsGovAdapter()
    # Wrong top-level type.
    assert adapter.extract([]) == []
    assert adapter.extract(None) == []
    # data present but oppHits missing / wrong type.
    assert adapter.extract({"data": {}}) == []
    assert adapter.extract({"data": {"oppHits": "nope"}}) == []
    assert adapter.extract({"data": None}) == []


def test_record_passes_the_gate_shape(monkeypatch):
    """The surviving record satisfies the gate (URLs unchecked, offline)."""
    from pipeline.validate import validate_record

    adapter = GrantsGovAdapter()
    monkeypatch.setattr(adapter, "fetch", lambda: _payload())
    records = adapter.extract(adapter.fetch())

    ok, reasons = validate_record(records[0], check_urls=False)
    assert ok, reasons
