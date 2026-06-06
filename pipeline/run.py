"""Pipeline entry point — fast, concurrent, multi-source runner.

Run from the repo root as::

    python -m pipeline.run                 # fetch, gate, merge, write
    python -m pipeline.run --dry-run       # do everything but skip the write
    python -m pipeline.run --data X.json --cache Y.json --max-workers 24

Concurrent flow (the whole point — wall-clock fast):

  1. validate.reset_url_cache()                          (fresh per-run memo)
  2. fetch ALL adapters in parallel (concurrent_map)     -> (adapter, raw)
  3. hash-check each (sequential, cheap) via HashCache   -> to_extract / skipped
  4. extract changed sources in parallel                 (parallelizes Gemini)
  5. flatten partials -> fill defaults + last_verified
  6. validate CONCURRENTLY (parallel HEAD checks, memoized)
  7. merge_records(existing, valid); write only if changed; save cache

Every adapter is isolated: a fetch or extract failure becomes an empty payload
or an empty record list, never an exception that sinks the run.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from pipeline import validate
from pipeline.cache import HashCache
from pipeline.http import concurrent_map
from pipeline.merge import merge_records
from pipeline.schema import RECORD_FIELDS, empty_record
from pipeline.sources.devpost import DevpostAdapter
from pipeline.sources.grants_gov import GrantsGovAdapter
from pipeline.sources.llm_accelerator import LlmAcceleratorAdapter
from pipeline.sources.sbir import SbirAdapter
from pipeline.validate import validate_record

# --- Paths ------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "src" / "data" / "deadlines.json"
REJECTED_LOG = ROOT / "pipeline" / "rejected.log"

# Best-effort .env load so local runs pick up GEMINI_API_KEY without crashing
# in environments where python-dotenv isn't installed.
try:  # pragma: no cover - trivial guard
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001 - dotenv is a convenience, never required
    pass

log = logging.getLogger("pipeline.run")

# --- Adapters ---------------------------------------------------------------
# Real accelerator/competition pages handed to the LLM adapter. The LiveVerify
# phase prunes any that don't yield records, so it's fine to list a few.
LLM_TARGETS = [
    # Tier-1 accelerators
    ("https://www.antler.co/apply", "Antler", "Antler Residency"),
    ("https://www.ycombinator.com/apply", "Y Combinator", "Y Combinator"),
    ("https://www.techstars.com/accelerators", "Techstars", "Techstars Accelerator"),
    ("https://www.500.co/accelerators", "500 Global", "500 Global Flagship"),
    # More US accelerators / residencies
    ("https://www.alchemistaccelerator.com/apply", "Alchemist Accelerator", "Alchemist Accelerator"),
    ("https://www.gener8tor.com/programs", "gener8tor", "gener8tor Accelerator"),
    ("https://hf0.com/", "HF0", "HF0 Residency"),
    ("https://www.spc.com/", "South Park Commons", "South Park Commons Founder Fellowship"),
    ("https://a16z.com/speedrun/", "Andreessen Horowitz", "a16z Speedrun"),
    ("https://www.foundersinc.com/", "Founders, Inc.", "Founders Inc / f.inc"),
    ("https://founderinstitute.com/apply/", "Founder Institute", "Founder Institute"),
    ("https://www.capitalfactory.com/", "Capital Factory", "Capital Factory Accelerator"),
    # Bio / deep-tech
    ("https://www.indiebio.co/", "IndieBio (SOSV)", "IndieBio"),
    # International accelerators
    ("https://seedcamp.com/", "Seedcamp", "Seedcamp"),
    ("https://www.foundersfactory.com/", "Founders Factory", "Founders Factory"),
    ("https://www.startupbootcamp.org/", "Startupbootcamp", "Startupbootcamp"),
    # Fellowships / non-dilutive
    ("https://www.spc.com/fellowship", "South Park Commons", "SPC Founder Fellowship"),
]

# The full adapter roster: three structured APIs + the LLM targets.
ADAPTERS = [
    SbirAdapter(),
    GrantsGovAdapter(),
    DevpostAdapter(),
    *[
        LlmAcceleratorAdapter(url=url, organizer=organizer, name_hint=name_hint)
        for (url, organizer, name_hint) in LLM_TARGETS
    ],
]


# --- Small helpers ----------------------------------------------------------


def _now_iso() -> str:
    """Current UTC time as an ISO 8601 'Z' string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_existing(path: Path) -> list[dict]:
    """Load the committed dataset, or return ``[]`` if missing/empty/bad."""
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read %s (%s); starting from empty", path, exc)
        return []


def _ordered(rec: dict) -> dict:
    """Return ``rec`` with keys in the canonical :data:`RECORD_FIELDS` order."""
    return {field: rec.get(field) for field in RECORD_FIELDS}


def _fill_defaults(partial: dict) -> dict:
    """Merge a partial record onto a fully-defaulted empty record."""
    rec = empty_record()
    for key, value in partial.items():
        if key in rec:
            rec[key] = value
    return rec


def _hashable_payload(raw) -> str:
    """Stable string form of a raw payload for content hashing."""
    if isinstance(raw, (str, bytes)):
        return raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
    return json.dumps(raw, sort_keys=True, default=str)


def _log_rejection(path: Path, name: str, reasons: list[str]) -> None:
    """Append one timestamped rejection line to the rejected log."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{_now_iso()}\t{name}\t{'; '.join(reasons)}\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


# --- Isolated per-adapter steps (never raise) -------------------------------


def _safe_fetch(adapter):
    """Call ``adapter.fetch()``; on ANY failure return an empty payload."""
    try:
        return adapter.fetch()
    except Exception as exc:  # noqa: BLE001 - isolate adapter fetch failures
        log.error("adapter %s fetch crashed (%s); empty payload", adapter.name, exc)
        return {}


def _safe_extract(adapter, raw) -> list[dict]:
    """Call ``adapter.extract(raw)``; on ANY failure return ``[]``.

    Tags each partial with its origin adapter name so rejection logs and the
    per-source summary stay accurate after the partials are flattened.
    """
    try:
        partials = adapter.extract(raw) or []
    except Exception as exc:  # noqa: BLE001 - isolate adapter extract failures
        log.error("adapter %s extract crashed (%s); dropping", adapter.name, exc)
        return []
    out: list[dict] = []
    for partial in partials:
        if isinstance(partial, dict):
            rec = _fill_defaults(partial)
            rec["last_verified"] = _now_iso()
            rec["_source"] = adapter.name  # internal tag, stripped before write
            out.append(rec)
    return out


# --- The run ----------------------------------------------------------------


def run(
    dry_run: bool = False,
    *,
    adapters: list | None = None,
    data_path: Path | str | None = None,
    cache_path: Path | str | None = None,
    rejected_path: Path | str | None = None,
    max_workers: int = 16,
) -> dict:
    """Execute the full pipeline once. Returns a summary dict of counts.

    All paths and the adapter roster are injectable so the LiveVerify phase (and
    tests) can run into scratch files without touching the committed dataset or
    the real hash-cache.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    adapters = ADAPTERS if adapters is None else list(adapters)
    data_path = DATA if data_path is None else Path(data_path)
    rejected_path = REJECTED_LOG if rejected_path is None else Path(rejected_path)

    existing = _load_existing(data_path)
    cache = HashCache() if cache_path is None else HashCache(path=cache_path)

    # 1) Fresh per-run URL reachability memo (thread-safe inside validate).
    validate.reset_url_cache()

    t0 = time.perf_counter()

    # 2) Fetch ALL adapters in parallel.
    fetched_pairs = concurrent_map(
        lambda a: (a, _safe_fetch(a)),
        adapters,
        max_workers=max_workers,
        label="fetch",
    )

    # 3) Hash-check each (sequential, cheap); collect changed sources.
    to_extract: list[tuple] = []
    skipped = 0
    per_source: dict[str, dict] = {}
    for pair in fetched_pairs:
        if pair is None:  # concurrent_map swallowed a catastrophic failure
            continue
        adapter, raw = pair
        per_source.setdefault(
            adapter.name, {"extracted": 0, "valid": 0, "rejected": 0, "skipped": False}
        )
        if not cache.changed(adapter.cache_key(), _hashable_payload(raw)):
            skipped += 1
            per_source[adapter.name]["skipped"] = True
            log.info("%s: skipped (unchanged)", adapter.name)
            continue
        to_extract.append((adapter, raw))

    # 4) Extract changed sources in parallel (parallelizes the Gemini calls).
    extracted = concurrent_map(
        lambda pr: _safe_extract(pr[0], pr[1]),
        to_extract,
        max_workers=max_workers,
        label="extract",
    )

    # 5) Flatten partials (defaults already filled + last_verified set).
    records: list[dict] = []
    for group in extracted:
        if group:
            records.extend(group)
    for rec in records:
        per_source[rec["_source"]]["extracted"] += 1

    net_seconds = time.perf_counter() - t0

    # 6) Validate CONCURRENTLY (parallel HEAD checks, memoized in validate).
    checked = concurrent_map(
        lambda r: (r, validate_record(r, check_urls=True)),
        records,
        max_workers=max_workers,
        label="validate",
    )

    collected: list[dict] = []
    rejected = 0
    for result in checked:
        if result is None:
            continue
        rec, (ok, reasons) = result
        source = rec["_source"]
        clean = {k: v for k, v in rec.items() if k != "_source"}
        if ok:
            collected.append(clean)
            per_source[source]["valid"] += 1
        else:
            rejected += 1
            per_source[source]["rejected"] += 1
            _log_rejection(
                rejected_path,
                clean.get("slug") or clean.get("name") or "?",
                reasons,
            )
            log.info("%s: rejected %r (%s)", source, clean.get("name"), reasons)

    total_seconds = time.perf_counter() - t0

    # 7) Merge and write-if-changed.
    merged, notes = merge_records(existing, collected)
    for note in notes:
        log.info("merge: %s", note)

    ordered = [_ordered(rec) for rec in merged]
    new_content = json.dumps(ordered, indent=2, ensure_ascii=False) + "\n"
    old_content = data_path.read_text(encoding="utf-8") if data_path.exists() else ""

    written = False
    if new_content != old_content:
        if dry_run:
            log.info("[dry-run] would write %d records to %s", len(ordered), data_path)
        else:
            data_path.parent.mkdir(parents=True, exist_ok=True)
            data_path.write_text(new_content, encoding="utf-8")
            written = True
            log.info("wrote %d records to %s", len(ordered), data_path)
    else:
        log.info("dataset unchanged; not rewriting %s", data_path)

    if not dry_run:
        cache.save()

    summary = {
        "adapters": len(adapters),
        "fetched": sum(1 for p in fetched_pairs if p is not None),
        "skipped": skipped,
        "extracted": len(records),
        "valid": len(collected),
        "rejected": rejected,
        "merged": len(merged),
        "written": written,
        "net_seconds": round(net_seconds, 3),
        "total_seconds": round(total_seconds, 3),
        "per_source": per_source,
    }

    # --- Pretty per-source summary ------------------------------------------
    log.info("=" * 60)
    log.info("SUMMARY (network phase %.2fs, total %.2fs)", net_seconds, total_seconds)
    for name in sorted(per_source):
        s = per_source[name]
        log.info(
            "  %-22s extracted=%d valid=%d rejected=%d%s",
            name,
            s["extracted"],
            s["valid"],
            s["rejected"],
            " [skipped:unchanged]" if s["skipped"] else "",
        )
    log.info(
        "  TOTAL  adapters=%d fetched=%d skipped=%d valid=%d rejected=%d "
        "merged=%d written=%s",
        summary["adapters"],
        summary["fetched"],
        summary["skipped"],
        summary["valid"],
        summary["rejected"],
        summary["merged"],
        summary["written"],
    )
    log.info("=" * 60)

    return summary


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Run the FundingDeadlines pipeline.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do everything except write the dataset file.",
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=None,
        help="Override the output dataset path (default src/data/deadlines.json).",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="Override the hash-cache path (default pipeline/cache/hashes.json).",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=16,
        help="Thread-pool size for fetch/extract/validate fan-out (default 16).",
    )
    args = parser.parse_args()
    run(
        dry_run=args.dry_run,
        data_path=args.data,
        cache_path=args.cache,
        max_workers=args.max_workers,
    )


if __name__ == "__main__":
    main()
