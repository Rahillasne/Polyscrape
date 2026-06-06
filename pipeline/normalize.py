"""Field normalizers: slugs, funding strings, and deadline parsing.

Everything here is deterministic and side-effect free so the same raw input
always produces the same canonical output (important for stable slugs and
idempotent merges).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from dateutil import parser as _dateparser

# --- Slugs ------------------------------------------------------------------

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def compute_slug(name: str, cycle: str | None = None) -> str:
    """Build a deterministic kebab-case slug from ``name`` (+ optional cycle).

    Lowercases, strips punctuation/acc" symbols, collapses runs of
    non-alphanumerics into single hyphens, and trims leading/trailing hyphens.
    Examples::

        compute_slug("Y Combinator", "Fall 2026")  -> "y-combinator-fall-2026"
        compute_slug("SBIR — Phase I!")             -> "sbir-phase-i"
    """
    parts = [name or ""]
    if cycle:
        parts.append(cycle)
    raw = " ".join(p for p in parts if p)
    slug = _SLUG_STRIP.sub("-", raw.lower())
    return slug.strip("-")


# --- Funding ----------------------------------------------------------------

# Matches a number with an optional k/m suffix, e.g. "500k", "1.5M", "100,000".
_MONEY_RE = re.compile(
    r"\$?\s*([0-9][0-9,]*\.?[0-9]*)\s*([kKmM])?",
)

# Phrases that pass through as descriptive funding terms rather than amounts.
_PASSTHROUGH = {
    "equity-free": "Equity-free",
    "equity free": "Equity-free",
    "non-dilutive": "Non-dilutive",
    "non dilutive": "Non-dilutive",
}


def normalize_funding(raw: str | None) -> str | None:
    """Normalize a funding string into a canonical ``$N,NNN`` form.

    Examples::

        "500k"        -> "$500,000"
        "$1.5M"       -> "$1,500,000"
        "1.5m"        -> "$1,500,000"
        "$100,000"    -> "$100,000"
        "equity-free" -> "Equity-free"
        None / ""     -> None
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None

    # Descriptive (non-amount) funding terms pass through, titled.
    key = text.lower().strip(" .")
    if key in _PASSTHROUGH:
        return _PASSTHROUGH[key]

    match = _MONEY_RE.search(text)
    if not match:
        # Nothing money-like; return the original trimmed string untouched.
        return text

    number_str, suffix = match.group(1), match.group(2)
    number_str = number_str.replace(",", "")
    try:
        amount = float(number_str)
    except ValueError:
        return text

    if suffix in ("k", "K"):
        amount *= 1_000
    elif suffix in ("m", "M"):
        amount *= 1_000_000

    return "$" + format(int(round(amount)), ",")


# --- Deadlines --------------------------------------------------------------


def parse_deadline(raw) -> str | None:
    """Parse many date representations into an ISO 8601 UTC string or ``None``.

    Accepts ISO strings, common human formats, and ``datetime`` objects.
    Naive datetimes are assumed to be UTC. Output always looks like
    ``"2026-08-12T03:00:00Z"``. Returns ``None`` on empty/unparseable input.
    """
    if raw is None:
        return None

    dt: datetime | None = None
    if isinstance(raw, datetime):
        dt = raw
    else:
        text = str(raw).strip()
        if not text:
            return None
        try:
            dt = _dateparser.parse(text)
        except (ValueError, OverflowError, TypeError):
            return None

    if dt is None:
        return None

    # Assume naive timestamps are UTC; otherwise convert to UTC.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    # Emit a trailing "Z" rather than "+00:00".
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
