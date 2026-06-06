"""Dataset link-health auditor — catch dead (404/410/DNS) links before they ship.

The committed dataset (``src/data/deadlines.json``) is what the static site
renders, and curated records bypass the pipeline's live URL gate. This standalone
auditor closes that gap: it probes every ``apply_url`` and ``source_url`` in the
dataset with the SAME lenient reachability policy the gate uses
(:func:`pipeline.validate.check_url_ok`) and reports the genuinely dead ones.

Usage::

    python -m pipeline.check_urls                       # audit the committed dataset
    python -m pipeline.check_urls --data path.json      # audit another file
    python -m pipeline.check_urls --json                # machine-readable report
    python -m pipeline.check_urls --strip               # rewrite file dropping dead-link records

Exit code is non-zero when any dead link is found, so CI can fail the build.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pipeline import validate
from pipeline.http import concurrent_map

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "src" / "data" / "deadlines.json"

_URL_FIELDS = ("apply_url", "source_url")


def _load(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, list) else []


def audit(records: list[dict], *, max_workers: int = 16) -> dict:
    """Probe every URL in ``records`` once (memoized) and classify each record.

    Returns ``{"dead": [...], "ok": [...], "checked_urls": int}`` where each dead
    entry is ``{"slug", "name", "field", "url"}``.
    """
    validate.reset_url_cache()

    # Collect the unique URL set so each distinct URL is only probed once.
    urls: set[str] = set()
    for rec in records:
        for field in _URL_FIELDS:
            url = rec.get(field)
            if isinstance(url, str) and url:
                urls.add(url)

    url_list = sorted(urls)
    results = concurrent_map(
        lambda u: (u, validate.check_url_ok(u)),
        url_list,
        max_workers=max_workers,
        label="check_urls",
    )
    status = {u: ok for pair in results if pair is not None for (u, ok) in [pair]}

    dead: list[dict] = []
    ok: list[dict] = []
    for rec in records:
        rec_dead = False
        for field in _URL_FIELDS:
            url = rec.get(field)
            if not isinstance(url, str) or not url:
                dead.append(
                    {"slug": rec.get("slug"), "name": rec.get("name"),
                     "field": field, "url": url, "reason": "missing/empty"}
                )
                rec_dead = True
                continue
            if not status.get(url, False):
                reason = "not https" if not url.startswith("https://") else "dead (404/410/DNS/unreachable)"
                dead.append(
                    {"slug": rec.get("slug"), "name": rec.get("name"),
                     "field": field, "url": url, "reason": reason}
                )
                rec_dead = True
        if not rec_dead:
            ok.append(rec)

    return {"dead": dead, "ok": ok, "checked_urls": len(url_list)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit dataset links for dead URLs.")
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--max-workers", type=int, default=16)
    parser.add_argument("--json", action="store_true", help="Emit a JSON report.")
    parser.add_argument(
        "--strip",
        action="store_true",
        help="Rewrite the dataset, dropping every record with a dead link.",
    )
    args = parser.parse_args()

    records = _load(args.data)
    report = audit(records, max_workers=args.max_workers)
    dead = report["dead"]

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"Checked {report['checked_urls']} unique URLs across {len(records)} records.")
        if not dead:
            print("All links healthy — no dead URLs. ✓")
        else:
            print(f"\n{len(dead)} dead link(s):\n")
            for d in dead:
                print(f"  [{d['reason']}] {d['slug']}  {d['field']}={d['url']}")

    if args.strip and dead:
        kept = report["ok"]
        new = json.dumps(kept, indent=2, ensure_ascii=False) + "\n"
        args.data.write_text(new, encoding="utf-8")
        print(f"\nStripped {len(records) - len(kept)} record(s); kept {len(kept)}.")

    sys.exit(1 if dead else 0)


if __name__ == "__main__":
    main()
