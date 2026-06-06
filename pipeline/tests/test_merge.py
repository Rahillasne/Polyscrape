"""Tests for the safe merge: completeness wins, no blanking, dedupe, bumps."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pipeline.merge import completeness, merge_records
from pipeline.schema import empty_record


def _future_iso(days: int = 60) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(slug="antler-fall-2026", **overrides) -> dict:
    rec = empty_record()
    rec.update(
        {
            "slug": slug,
            "name": "Antler — Fall 2026",
            "category": "accelerator",
            "organizer": "Antler",
            "deadline": _future_iso(),
            "rolling": False,
            "location": "New York, NY",
            "funding": "$100,000",
            "stage": "pre-seed",
            "apply_url": "https://www.antler.co/apply",
            "source_url": "https://www.antler.co/apply",
            "source_type": "llm",
            "confidence": "high",
            "last_verified": "2020-01-01T00:00:00Z",
        }
    )
    rec.update(overrides)
    return rec


def test_completeness_counts_real_fields():
    full = _record()
    sparse = _record(funding="", location="", event_date=None)
    assert completeness(full) > completeness(sparse)


def test_new_slug_is_added():
    merged, notes = merge_records([], [_record()])
    assert len(merged) == 1
    assert merged[0]["slug"] == "antler-fall-2026"


def test_more_complete_record_overwrites():
    old = _record(funding="", location="")  # less complete existing
    new = _record(funding="$100,000", location="New York, NY")  # richer
    merged, notes = merge_records([old], [new])
    assert len(merged) == 1
    assert merged[0]["funding"] == "$100,000"
    assert merged[0]["location"] == "New York, NY"
    assert any("overwrote" in n for n in notes)


def test_less_complete_record_does_not_blank_good_entry():
    good = _record(funding="$100,000", location="New York, NY", event_date="2026-10-01")
    sparse = _record(funding="", location="", event_date=None)
    merged, notes = merge_records([good], [sparse])
    assert len(merged) == 1
    # The good data survives — a broken/sparse scrape must NOT blank it.
    assert merged[0]["funding"] == "$100,000"
    assert merged[0]["location"] == "New York, NY"
    assert merged[0]["event_date"] == "2026-10-01"
    assert any("kept existing" in n for n in notes)


def test_duplicate_slugs_within_incoming_rejected():
    a = _record(funding="$100,000")
    b = _record(funding="$250,000")  # same slug, second occurrence
    merged, notes = merge_records([], [a, b])
    assert len(merged) == 1
    # First wins; second is dropped and logged.
    assert merged[0]["funding"] == "$100,000"
    assert any("duplicate slug within incoming" in n for n in notes)


def test_last_verified_bumped_on_overwrite():
    old = _record(funding="", last_verified="2020-01-01T00:00:00Z")
    new = _record(funding="$100,000", last_verified="2020-01-01T00:00:00Z")
    merged, _ = merge_records([old], [new])
    assert merged[0]["last_verified"] != "2020-01-01T00:00:00Z"
    # And it should be a recent UTC stamp.
    bumped = datetime.strptime(
        merged[0]["last_verified"], "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=timezone.utc)
    assert (datetime.now(timezone.utc) - bumped) < timedelta(minutes=5)


def test_last_verified_bumped_when_kept():
    # Even when the existing record is kept (incoming was worse), it was
    # re-confirmed and so its last_verified should be bumped.
    good = _record(funding="$100,000", last_verified="2020-01-01T00:00:00Z")
    sparse = _record(funding="")
    merged, _ = merge_records([good], [sparse])
    assert merged[0]["last_verified"] != "2020-01-01T00:00:00Z"


def test_invalid_incoming_not_merged():
    # A record failing the gate (bad category) must never enter the dataset.
    bad = _record(category="bogus")
    merged, notes = merge_records([], [bad])
    assert merged == []
    assert any("failed gate" in n for n in notes)


def test_curated_record_protected_from_noncurated_overwrite():
    # A hand-curated seed entry must NEVER be replaced by a scraped/LLM record
    # sharing its slug, even when the newcomer is more complete.
    curated = _record(
        source_type="curated",
        name="Antler Founder Residency",
        funding="",  # deliberately less complete than the incoming LLM record
    )
    richer_llm = _record(
        source_type="llm",
        name="Antler Residency",
        funding="$100,000",
        event_date="2026-10-01",
    )
    assert completeness(richer_llm) > completeness(curated)
    merged, notes = merge_records([curated], [richer_llm])
    assert len(merged) == 1
    assert merged[0]["source_type"] == "curated"
    assert merged[0]["name"] == "Antler Founder Residency"
    assert any("kept curated" in n for n in notes)


def test_curated_record_can_be_updated_by_curated():
    # A curated update (e.g. re-running the seed) may still refresh a curated row.
    old = _record(source_type="curated", funding="")
    new = _record(source_type="curated", funding="$100,000")
    merged, notes = merge_records([old], [new])
    assert merged[0]["funding"] == "$100,000"
    assert any("overwrote" in n for n in notes)
