"""Tests for the Devpost adapter (offline — monkeypatched fetch, no network).

``fetch`` is replaced with a fixed payload containing one valid upcoming
hackathon, one with an unparseable date, and one whose deadline is in the past.
``extract`` must keep only the valid future one and map it correctly.
"""

from __future__ import annotations

from pipeline.sources.devpost import DevpostAdapter

# Today (per the run context) is 2026-06-06, so 2026-08-15 is future and any
# 2025 date is past.
_FAKE_PAYLOAD = {
    "hackathons": [
        {
            # Valid, upcoming, online, with a prize.
            "title": "Global AI Pitch Hack",
            "url": "https://global-ai-pitch.devpost.com",
            "submission_period_dates": "Jun 01 - Aug 15, 2026",
            "prize_amount": "<span data-currency-value>50,000</span> in prizes",
            "displayed_location": {"location": "Online"},
            "open_state": "upcoming",
            "organization_name": "Devpost Labs",
        },
        {
            # Unparseable submission window -> skipped (not marked rolling).
            "title": "Mystery Date Jam",
            "url": "https://mystery-jam.devpost.com",
            "submission_period_dates": "Coming soon - to be announced",
            "prize_amount": "<span>10,000</span>",
            "displayed_location": {"location": "San Francisco, CA"},
            "open_state": "open",
            "organization_name": "Mystery Co",
        },
        {
            # Past deadline -> skipped.
            "title": "Last Year Throwback Hack",
            "url": "https://throwback.devpost.com",
            "submission_period_dates": "Jan 01 - Mar 15, 2025",
            "prize_amount": "<span>25,000</span>",
            "displayed_location": {"location": "New York, NY"},
            "open_state": "open",
            "organization_name": "Throwback Inc",
        },
    ]
}


def test_extract_keeps_only_valid_future(monkeypatch):
    adapter = DevpostAdapter()
    monkeypatch.setattr(DevpostAdapter, "fetch", lambda self: _FAKE_PAYLOAD)

    records = adapter.extract(adapter.fetch())

    # Only the valid, future, parseable entry survives.
    assert len(records) == 1
    rec = records[0]

    assert rec["name"] == "Global AI Pitch Hack"
    assert rec["category"] == "competition"
    assert rec["source_type"] == "api"
    assert rec["rolling"] is False
    assert rec["stage"] == "any"

    # https devpost url used for both apply + source.
    assert rec["apply_url"] == "https://global-ai-pitch.devpost.com"
    assert rec["source_url"] == "https://global-ai-pitch.devpost.com"

    # Deadline = END of window, parsed to ISO8601-Z and in the future.
    assert rec["deadline"] == "2026-08-15T00:00:00Z"

    # Funding normalized from the prize HTML digits.
    assert rec["funding"] == "$50,000"

    # "Online" location maps to "Remote".
    assert rec["location"] == "Remote"

    # Slug derived from title + deadline year.
    assert rec["slug"] == "global-ai-pitch-hack-2026"

    # Online location is not a concrete place, so confidence is medium.
    assert rec["confidence"] == "medium"

    assert rec["needs_review"] is False


def test_high_confidence_with_concrete_location(monkeypatch):
    payload = {
        "hackathons": [
            {
                "title": "Bay Area Builders Cup",
                "url": "https://bay-builders.devpost.com",
                "submission_period_dates": "Jul 01 - Sep 30, 2026",
                "prize_amount": "<span>100,000</span>",
                "displayed_location": {"location": "San Francisco, CA"},
                "open_state": "upcoming",
                "organization_name": "Builders Guild",
            }
        ]
    }
    adapter = DevpostAdapter()
    records = adapter.extract(payload)

    assert len(records) == 1
    rec = records[0]
    assert rec["location"] == "San Francisco, CA"
    assert rec["funding"] == "$100,000"
    assert rec["organizer"] == "Builders Guild"
    # deadline + funding + concrete location all present -> high.
    assert rec["confidence"] == "high"


def test_skips_non_https_url():
    payload = {
        "hackathons": [
            {
                "title": "Insecure Hack",
                "url": "http://insecure.devpost.com",
                "submission_period_dates": "Jul 01 - Sep 30, 2026",
                "prize_amount": "<span>5,000</span>",
                "displayed_location": {"location": "Online"},
            }
        ]
    }
    assert DevpostAdapter().extract(payload) == []


def test_empty_dict_yields_empty():
    assert DevpostAdapter().extract({}) == []


def test_non_dict_payload_yields_empty():
    # Defensive: unexpected structures never crash, just return [].
    assert DevpostAdapter().extract([]) == []
    assert DevpostAdapter().extract(None) == []
    assert DevpostAdapter().extract({"hackathons": "not-a-list"}) == []


def test_fetch_returns_payload_when_monkeypatched(monkeypatch):
    monkeypatch.setattr(DevpostAdapter, "fetch", lambda self: _FAKE_PAYLOAD)
    assert DevpostAdapter().fetch() is _FAKE_PAYLOAD
