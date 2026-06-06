# Polyscrape — FundingDeadlines

A static site that tracks deadlines for **accelerators, pitch competitions, grants, and
fellowships** — like [conferencedeadlines.com](https://conferencedeadlines.com) but for
startup funding. Sortable by category, live countdown per deadline, one-click `.ics` export.

The product is the **data layer**: a robust, fast, multi-source scraper feeding a strict
data-quality gate. The frontend just renders one committed JSON file.

```
pipeline/                  Python scraper + quality gate + safe merge (the hard part)
src/                       Astro + TypeScript static frontend
src/data/deadlines.json    the committed dataset the site renders (~30 curated + scraped)
.github/workflows/         daily cron that refreshes the dataset
```

- **Frontend:** Astro + TypeScript, static output, vanilla client-side TS. No backend, no DB.
- **Pipeline:** Python 3.11+, runs on a daily GitHub Actions cron.
- **AI:** Gemini Flash (`gemini-2.5-flash`) only, **structured output** (JSON schema — no regex),
  only in the cron, only on **changed** pages (sha256 cache). Cost target ≈ $0/month.
- **Hosting:** Cloudflare Pages or Vercel free tier.

---

## Quick start

### Pipeline (Python)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r pipeline/requirements.txt

cp pipeline/.env.example .env          # then put your key in .env
echo 'GEMINI_API_KEY=your-key-here' >> .env

python -m pipeline.run                 # fetch -> gate -> merge -> write deadlines.json
python -m pipeline.run --dry-run       # do everything except write
python -m pytest pipeline/tests -q     # 63 tests, fully offline (no key, no network)
```

CLI flags:

| Flag | Default | Purpose |
|------|---------|---------|
| `--dry-run` | off | Run everything but don't write the dataset. |
| `--data PATH` | `src/data/deadlines.json` | Output dataset path. |
| `--cache PATH` | `pipeline/cache/hashes.json` | Page-hash cache path. |
| `--max-workers N` | `16` | Thread-pool size for the fetch/extract/validate fan-out. |

> The pipeline runs **without** a Gemini key — the LLM adapter logs and skips cleanly, the
> API/scrape adapters still run. The key only unlocks LLM extraction from accelerator pages.

### Frontend (Astro)

```bash
npm install
npm run dev        # local dev server
npm run build      # static build -> dist/
npm run preview    # preview the build
```

Set your repo in [src/config.ts](src/config.ts) so the **"Report wrong date"** links and
footer point at your GitHub:

```ts
export const GITHUB_REPO = "Rahillasne/Polyscrape";
```

---

## How the pipeline works

```
for each adapter (in parallel):
    fetch()                      -> raw payload   (pooled, retrying HTTP session)
    sha256(raw) unchanged?       -> skip extract  (so Gemini only runs on changed pages)
    extract(raw)                 -> partial records
fill defaults + last_verified
validate every record (in parallel, memoized URL checks)   -> the quality GATE
    valid   -> collected
    invalid -> appended to pipeline/rejected.log and DROPPED
merge_records(existing, collected)   -> never destroys good data
write src/data/deadlines.json only if it actually changed
```

Everything that touches the network goes through [pipeline/http.py](pipeline/http.py):
a connection-pooled `requests.Session` with **exponential-backoff retries** on 429/5xx
(this is what makes flaky sources like SBIR self-heal), a default timeout on every call,
and a `concurrent_map()` thread-pool helper. Fetch, extract (incl. parallel Gemini calls),
and per-record URL validation all fan out across the pool — a full multi-source run is
network-bound and finishes in ~20s, not the sum of every source.

### Sources (adapters)

| Adapter | File | Type | Category | Notes |
|---------|------|------|----------|-------|
| SBIR.gov | [sbir.py](pipeline/sources/sbir.py) | `api` | grant | Open SBIR/STTR solicitations. |
| Grants.gov | [grants_gov.py](pipeline/sources/grants_gov.py) | `api` | grant | Search2 API, innovation/tech grants. |
| Devpost | [devpost.py](pipeline/sources/devpost.py) | `api` | competition | Open/upcoming hackathons feed. |
| Accelerator pages | [llm_accelerator.py](pipeline/sources/llm_accelerator.py) | `llm` | any | Gemini Flash structured extraction (Antler, YC, Techstars, 500 Global). |

### The quality gate — [validate.py](pipeline/validate.py)

A record is **dropped** (and logged to `pipeline/rejected.log`) unless **all** hold:

- `name`, `category`, `organizer`, `apply_url`, `source_url` are present and non-empty.
- `category ∈ {accelerator, competition, grant, fellowship}`, `stage ∈ {pre-seed, seed, any}`,
  `source_type ∈ {api, scrape, llm, curated}`.
- `deadline` parses to valid ISO 8601 **or** `rolling: true`; non-rolling deadlines may not be
  in the distant past (>30 days ago).
- `apply_url` / `source_url` are `https://` and the host is reachable. The reachability check
  is **lenient on purpose**: a host that answers `401/403/405/429` (bot-blocking) or `5xx`
  (transient) is treated as alive, so legitimately bot-hostile program pages aren't dropped;
  only genuinely dead links (`404/410`, DNS/connection failure) fail.

Confidence: `api`/`curated` → `high`; LLM records score confidence from how many fields came
back clean, and flag `needs_review: true` when the deadline was ambiguous.

### Safe merge — [merge.py](pipeline/merge.py)

Keyed by `slug`. **A broken or thinner scrape can never blank out a good entry.** A new record
overwrites an existing one only if it passes the gate **and** is at least as complete; otherwise
the existing record is kept (and its `last_verified` refreshed). **Curated entries are
sacrosanct** — an `api`/`llm`/`scrape` record can never overwrite a hand-curated one sharing its
slug. Duplicate slugs within a run are rejected.

---

## Data model

`src/data/deadlines.json` is an array of objects (16 keys, in order):

```jsonc
{
  "slug": "yc-fall-2026",                 // stable key: name+cycle, kebab-case
  "name": "Y Combinator — Fall 2026",
  "category": "accelerator",              // accelerator | competition | grant | fellowship
  "organizer": "Y Combinator",
  "deadline": "2026-08-12T03:00:00Z",     // ISO 8601 UTC, or null if rolling
  "rolling": false,
  "event_date": "2026-10-01",             // ISO date or null
  "location": "San Francisco, CA",        // or "Remote"
  "funding": "$500,000",                  // human string
  "stage": "any",                         // pre-seed | seed | any
  "apply_url": "https://...",
  "source_url": "https://...",
  "source_type": "api",                   // api | scrape | llm | curated
  "last_verified": "2026-06-06T00:00:00Z",
  "confidence": "high",                   // high | medium | low
  "needs_review": false
}
```

This shape is the single contract shared by the pipeline ([schema.py](pipeline/schema.py)),
the seed data, and the frontend ([src/types.ts](src/types.ts)).

---

## Adding a new source adapter

1. Create `pipeline/sources/<your_source>.py`:

   ```python
   from pipeline.http import make_session
   from pipeline.normalize import compute_slug, parse_deadline
   from pipeline.sources.base import ApiAdapter   # or ScrapeAdapter / LlmAdapter

   class YourAdapter(ApiAdapter):          # sets source_type = "api"
       name = "your_source"

       def fetch(self):
           # use make_session() for pooling + retries; return [] / {} / "" on failure
           return make_session().get("https://...").json()

       def extract(self, raw):
           # return a list of PARTIAL record dicts (any subset of the 16 keys).
           # The runner fills defaults, sets last_verified, and runs the gate.
           return [{ "name": ..., "category": "grant", "organizer": ...,
                     "deadline": parse_deadline(...), "apply_url": "https://...",
                     "source_url": "https://...", "slug": compute_slug(...) }]
   ```

   Rules: `fetch()` and `extract()` must **never raise** — return an empty payload/list on
   failure (the runner also isolates each adapter). Emit `https://` URLs and a future
   `deadline` (or `rolling: true`) so records can pass the gate.

2. Register it in [pipeline/run.py](pipeline/run.py)'s `ADAPTERS` list (LLM page targets go in
   `LLM_TARGETS`).
3. Add an **offline** test under `pipeline/tests/` that monkeypatches `fetch()` and asserts
   `extract()` maps fields correctly (see `test_grants_gov.py` / `test_devpost.py`).

---

## Frontend

Single landing page ([src/pages/index.astro](src/pages/index.astro)): hero, category tabs
(All / Accelerators / Competitions / Grants / Fellowships), text search, sort-by-soonest, and a
card per program with a **live countdown**, funding, location, stage chip, Apply button, a
`needs review` tag, per-card and per-category **`.ics` export** (generated client-side), and a
**"Report wrong date"** link that opens a pre-filled GitHub issue. Dark/light toggle, mobile-first.

Design system (warm editorial, Anthropic-style) lives in
[src/styles/global.css](src/styles/global.css) — ivory `#F0EEE6`, clay accent `#D97757`,
Spectral serif headings + Spline Sans body. No purple, no neon, no gradients.

---

## Deployment

Static build — point Cloudflare Pages or Vercel at the repo:

- **Build command:** `npm run build`
- **Output directory:** `dist`

The site is fully static; Gemini is never called at request time.

---

## Configuration / secrets

| Where | Key | Notes |
|-------|-----|-------|
| Local | `.env` → `GEMINI_API_KEY` | Loaded by the pipeline via `python-dotenv`. Gitignored. |
| GitHub Actions | repo secret `GEMINI_API_KEY` | Used by [refresh.yml](.github/workflows/refresh.yml). |
| Frontend | `GITHUB_REPO` in `src/config.ts` | Powers the "Report wrong date" issue links. |

The daily workflow ([.github/workflows/refresh.yml](.github/workflows/refresh.yml)) installs
deps, runs `python -m pipeline.run`, and commits `src/data/deadlines.json` **only when it
changes**.
