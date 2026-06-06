"""Tests that PROVE the data-quality gate works (offline, check_urls=False)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pipeline.schema import empty_record
from pipeline.validate import is_distant_past, validate_record


def _future_iso(days: int = 60) -> str:
    """An ISO 'Z' timestamp ``days`` in the future."""
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _past_iso(days: int = 90) -> str:
    """An ISO 'Z' timestamp ``days`` in the past."""
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _good_record() -> dict:
    """A fully valid record (future, non-rolling, https urls)."""
    rec = empty_record()
    rec.update(
        {
            "slug": "antler-fall-2026",
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
        }
    )
    return rec


# --- A valid record passes --------------------------------------------------


def test_valid_record_passes():
    ok, reasons = validate_record(_good_record(), check_urls=False)
    assert ok, reasons
    assert reasons == []


def test_valid_rolling_record_passes_without_deadline():
    rec = _good_record()
    rec["deadline"] = None
    rec["rolling"] = True
    ok, reasons = validate_record(rec, check_urls=False)
    assert ok, reasons


# --- Junk is rejected with reasons -----------------------------------------


def test_missing_required_field_rejected():
    rec = _good_record()
    rec["organizer"] = ""
    ok, reasons = validate_record(rec, check_urls=False)
    assert not ok
    assert any("organizer" in r for r in reasons)


def test_bad_category_rejected():
    rec = _good_record()
    rec["category"] = "vc-fund"
    ok, reasons = validate_record(rec, check_urls=False)
    assert not ok
    assert any("category" in r for r in reasons)


def test_bad_stage_rejected():
    rec = _good_record()
    rec["stage"] = "series-z"
    ok, reasons = validate_record(rec, check_urls=False)
    assert not ok
    assert any("stage" in r for r in reasons)


def test_non_https_url_rejected():
    rec = _good_record()
    rec["apply_url"] = "http://www.antler.co/apply"
    ok, reasons = validate_record(rec, check_urls=False)
    assert not ok
    assert any("apply_url is not https" in r for r in reasons)


def test_distant_past_deadline_non_rolling_rejected():
    rec = _good_record()
    rec["deadline"] = _past_iso(90)
    rec["rolling"] = False
    ok, reasons = validate_record(rec, check_urls=False)
    assert not ok
    assert any("distant past" in r for r in reasons)


def test_none_deadline_non_rolling_rejected():
    rec = _good_record()
    rec["deadline"] = None
    rec["rolling"] = False
    ok, reasons = validate_record(rec, check_urls=False)
    assert not ok
    assert any("rolling is False" in r for r in reasons)


def test_bad_source_type_rejected():
    rec = _good_record()
    rec["source_type"] = "magic"
    ok, reasons = validate_record(rec, check_urls=False)
    assert not ok
    assert any("source_type" in r for r in reasons)


def test_reasons_accumulate_all_failures():
    # A record that violates several rules at once should list all of them.
    rec = empty_record()
    rec.update(
        {
            "name": "",
            "category": "bogus",
            "stage": "nope",
            "deadline": None,
            "rolling": False,
            "apply_url": "http://x",
            "source_url": "ftp://y",
        }
    )
    ok, reasons = validate_record(rec, check_urls=False)
    assert not ok
    assert len(reasons) >= 5


# --- is_distant_past unit checks -------------------------------------------


def test_is_distant_past_true_for_old():
    assert is_distant_past(_past_iso(90)) is True


def test_is_distant_past_false_for_future():
    assert is_distant_past(_future_iso(30)) is False


def test_is_distant_past_false_for_unparseable():
    assert is_distant_past("garbage") is False
