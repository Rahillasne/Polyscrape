"""Tests for normalize: funding strings, slug determinism, deadline parsing."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pipeline.normalize import compute_slug, normalize_funding, parse_deadline


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("500k", "$500,000"),
        ("$500k", "$500,000"),
        ("1.5M", "$1,500,000"),
        ("1.5m", "$1,500,000"),
        ("$1.5M", "$1,500,000"),
        ("$100,000", "$100,000"),
        ("100000", "$100,000"),
        ("equity-free", "Equity-free"),
        ("Equity Free", "Equity-free"),
        ("non-dilutive", "Non-dilutive"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_funding(raw, expected):
    assert normalize_funding(raw) == expected


def test_slug_determinism_and_stability():
    # Same inputs always produce the same slug.
    a = compute_slug("Y Combinator", "Fall 2026")
    b = compute_slug("Y Combinator", "Fall 2026")
    assert a == b == "y-combinator-fall-2026"


def test_slug_strips_punctuation_and_collapses():
    assert compute_slug("SBIR — Phase I!!!") == "sbir-phase-i"
    assert compute_slug("  Antler   Residency  ") == "antler-residency"
    assert compute_slug("Techstars (NYC) 2026") == "techstars-nyc-2026"


def test_slug_without_cycle():
    assert compute_slug("Y Combinator") == "y-combinator"


def test_parse_deadline_iso():
    assert parse_deadline("2026-08-12T03:00:00Z") == "2026-08-12T03:00:00Z"


def test_parse_deadline_date_only_assumed_utc():
    assert parse_deadline("2026-08-12") == "2026-08-12T00:00:00Z"


def test_parse_deadline_datetime_object_naive():
    dt = datetime(2026, 8, 12, 3, 0, 0)
    assert parse_deadline(dt) == "2026-08-12T03:00:00Z"


def test_parse_deadline_aware_converted_to_utc():
    dt = datetime(2026, 8, 12, 3, 0, 0, tzinfo=timezone.utc)
    assert parse_deadline(dt) == "2026-08-12T03:00:00Z"


def test_parse_deadline_none_and_empty():
    assert parse_deadline(None) is None
    assert parse_deadline("") is None
    assert parse_deadline("   ") is None


def test_parse_deadline_garbage():
    assert parse_deadline("not a date at all !!!") is None
