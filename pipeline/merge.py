"""Safe merge of freshly-scraped records into the existing dataset.

The golden rule: a broken or less-complete scrape can NEVER blank out a good
existing entry. A new record only overwrites an old one if it passes the gate
*and* is at least as complete.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pipeline.schema import RECORD_FIELDS
from pipeline.validate import validate_record


def completeness(rec: dict) -> int:
    """Count how many of the record's fields carry real information.

    A field counts if it is present and not ``None``, not an empty/whitespace
    string. Booleans always count (``False`` is meaningful). This gives a cheap
    "how much do we know" score used to compare candidate records.
    """
    score = 0
    for field in RECORD_FIELDS:
        value = rec.get(field)
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                score += 1
        elif isinstance(value, bool):
            score += 1
        else:
            # Numbers or other truthy-by-presence values.
            score += 1
    return score


def _now_iso() -> str:
    """Current UTC time as an ISO 8601 'Z' string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def merge_records(
    existing: list[dict], incoming: list[dict]
) -> tuple[list[dict], list[str]]:
    """Merge ``incoming`` records into ``existing``, keyed by slug.

    Behaviour:
      * Start from ``existing`` (load order preserved).
      * Duplicate slugs *within* ``incoming`` are rejected (first wins, rest
        logged).
      * A new record overwrites an existing one ONLY IF it is valid
        (``check_urls=False`` — URLs were already checked upstream) AND its
        completeness is >= the existing record's completeness. Otherwise the
        existing record is kept untouched.
      * New slugs not seen before are appended (if valid).
      * ``last_verified`` is bumped to now (UTC) on every confirmed or
        overwritten record.

    Returns ``(merged_list, notes)``.
    """
    notes: list[str] = []
    now = _now_iso()

    # Preserve existing load order via a list of slugs + a lookup map.
    order: list[str] = []
    by_slug: dict[str, dict] = {}
    for rec in existing:
        slug = rec.get("slug", "")
        if slug in by_slug:
            notes.append(f"existing dataset already had duplicate slug: {slug!r}")
            continue
        order.append(slug)
        by_slug[slug] = rec

    seen_incoming: set[str] = set()

    for rec in incoming:
        slug = rec.get("slug", "")
        if not slug:
            notes.append(f"incoming record has no slug, skipped: {rec.get('name')!r}")
            continue

        # Reject duplicate slugs within this run.
        if slug in seen_incoming:
            notes.append(f"duplicate slug within incoming, dropped: {slug!r}")
            continue
        seen_incoming.add(slug)

        ok, reasons = validate_record(rec, check_urls=False)
        if not ok:
            notes.append(f"incoming {slug!r} failed gate, not merged: {reasons}")
            continue

        if slug not in by_slug:
            # Brand new, valid entry.
            new_rec = dict(rec)
            new_rec["last_verified"] = now
            by_slug[slug] = new_rec
            order.append(slug)
            notes.append(f"added new record: {slug!r}")
            continue

        # Slug exists. Curated entries are sacrosanct: a scraped/api/llm record
        # can NEVER overwrite a hand-curated one (it may still refresh the
        # timestamp). This guarantees the human-vetted seed survives every run.
        old = by_slug[slug]
        if old.get("source_type") == "curated" and rec.get("source_type") != "curated":
            old["last_verified"] = now
            notes.append(
                f"kept curated {slug!r} "
                f"(protected from {rec.get('source_type')!r} overwrite)"
            )
            continue

        # Otherwise overwrite only if the newcomer is at least as complete.
        if completeness(rec) >= completeness(old):
            new_rec = dict(rec)
            new_rec["last_verified"] = now
            by_slug[slug] = new_rec
            notes.append(
                f"overwrote {slug!r} "
                f"(completeness {completeness(old)} -> {completeness(rec)})"
            )
        else:
            # Keep the better existing entry, but it was re-confirmed: bump it.
            old["last_verified"] = now
            notes.append(
                f"kept existing {slug!r} "
                f"(incoming less complete: {completeness(rec)} < "
                f"{completeness(old)})"
            )

    merged = [by_slug[slug] for slug in order]
    return merged, notes
