"""Offline tests for the concurrent runner (no network, no API key).

These exercise the full ``run()`` flow with stub adapters and the URL
reachability check stubbed out, proving:

  * a valid record from a good adapter is written,
  * an invalid record from a junk adapter is rejected (logged + counted),
  * an existing CURATED record in the seed file is preserved (never blanked),
  * a second run with unchanged inputs is idempotent (writes nothing new).

Nothing here touches the network or the committed dataset: every run goes into
``tmp_path`` data/cache/rejected files, and ``validate.check_url_ok`` is forced
to ``True`` so no HEAD requests are made.
"""

from __future__ import annotations

import json

import pytest

from pipeline import run as run_mod
from pipeline import validate
from pipeline.sources.base import Adapter

# A safely-future deadline relative to the harness "today" (2026-06-06).
FUTURE_DEADLINE = "2026-12-31T00:00:00Z"


class GoodAdapter(Adapter):
    """Yields exactly one fully-valid record."""

    name = "good"
    source_type = "api"

    def fetch(self):
        return {"ok": True}

    def extract(self, raw):
        return [
            {
                "slug": "good-program-2026",
                "name": "Good Program 2026",
                "category": "accelerator",
                "organizer": "Good Org",
                "deadline": FUTURE_DEADLINE,
                "rolling": False,
                "location": "Remote",
                "funding": "$100,000",
                "stage": "seed",
                "apply_url": "https://example.com/apply",
                "source_url": "https://example.com/program",
                "source_type": "api",
                "confidence": "high",
                "needs_review": False,
            }
        ]


class JunkAdapter(Adapter):
    """Yields one record that must FAIL the gate (bad category + past deadline)."""

    name = "junk"
    source_type = "api"

    def fetch(self):
        return {"ok": True}

    def extract(self, raw):
        return [
            {
                "slug": "junk-thing",
                "name": "Junk Thing",
                "category": "not-a-real-category",  # invalid enum
                "organizer": "Junk Org",
                "deadline": "2000-01-01T00:00:00Z",  # distant past
                "rolling": False,
                "apply_url": "https://example.com/junk",
                "source_url": "https://example.com/junk",
                "source_type": "api",
            }
        ]


# A pre-existing CURATED record that lives in the seed file and must survive.
SEED_CURATED = {
    "slug": "curated-keepme-2026",
    "name": "Curated Keep Me 2026",
    "category": "grant",
    "organizer": "Curators Inc",
    "deadline": FUTURE_DEADLINE,
    "rolling": False,
    "event_date": None,
    "location": "Remote",
    "funding": "$50,000",
    "stage": "any",
    "apply_url": "https://example.com/curated-apply",
    "source_url": "https://example.com/curated-source",
    "source_type": "curated",
    "last_verified": "2026-06-01T00:00:00Z",
    "confidence": "high",
    "needs_review": False,
}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Force every URL reachability check to pass — keeps the suite offline."""
    monkeypatch.setattr(validate, "check_url_ok", lambda url, timeout=12.0: True)


def _write_seed(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([SEED_CURATED], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def test_good_written_junk_rejected_curated_preserved(tmp_path, monkeypatch):
    data_path = tmp_path / "deadlines.json"
    cache_path = tmp_path / "hashes.json"
    rejected_path = tmp_path / "rejected.log"
    _write_seed(data_path)

    # Inject just our stub adapters (also covers the module-level ADAPTERS path).
    monkeypatch.setattr(run_mod, "ADAPTERS", [GoodAdapter(), JunkAdapter()])

    summary = run_mod.run(
        adapters=run_mod.ADAPTERS,
        data_path=data_path,
        cache_path=cache_path,
        rejected_path=rejected_path,
        max_workers=4,
    )

    # The junk record was rejected (counted + logged).
    assert summary["rejected"] >= 1
    assert rejected_path.exists()
    assert "junk" in rejected_path.read_text(encoding="utf-8").lower()

    # The dataset was written and the good record is present.
    assert summary["written"] is True
    written = json.loads(data_path.read_text(encoding="utf-8"))
    slugs = {rec["slug"] for rec in written}
    assert "good-program-2026" in slugs

    # The junk record never made it into the dataset.
    assert "junk-thing" not in slugs

    # The existing curated record is preserved (not blanked / dropped).
    assert "curated-keepme-2026" in slugs
    curated = next(r for r in written if r["slug"] == "curated-keepme-2026")
    assert curated["source_type"] == "curated"
    assert curated["name"] == "Curated Keep Me 2026"
    assert curated["apply_url"] == "https://example.com/curated-apply"

    # Canonical key ordering on write.
    from pipeline.schema import RECORD_FIELDS

    for rec in written:
        assert list(rec.keys()) == RECORD_FIELDS


def test_second_run_is_idempotent(tmp_path, monkeypatch):
    data_path = tmp_path / "deadlines.json"
    cache_path = tmp_path / "hashes.json"
    rejected_path = tmp_path / "rejected.log"
    _write_seed(data_path)

    adapters = [GoodAdapter(), JunkAdapter()]
    monkeypatch.setattr(run_mod, "ADAPTERS", adapters)

    kwargs = dict(
        adapters=adapters,
        data_path=data_path,
        cache_path=cache_path,
        rejected_path=rejected_path,
        max_workers=4,
    )

    first = run_mod.run(**kwargs)
    assert first["written"] is True
    content_after_first = data_path.read_text(encoding="utf-8")

    # Second run: inputs unchanged -> hash cache skips extraction, nothing new
    # gets written and the file is byte-for-byte identical.
    second = run_mod.run(**kwargs)
    assert second["written"] is False
    assert second["skipped"] >= 1  # both stub sources hashed unchanged
    assert data_path.read_text(encoding="utf-8") == content_after_first
