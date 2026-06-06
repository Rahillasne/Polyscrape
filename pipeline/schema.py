"""The shared data contract for every part of the pipeline.

Defines the record shape (16 ordered keys), the controlled vocabularies
(enums), a factory for a fully-defaulted empty record, and the JSON Schema
handed to Gemini for structured output.
"""

from __future__ import annotations

# --- Controlled vocabularies (enums) ----------------------------------------

CATEGORIES = {"accelerator", "competition", "grant", "fellowship"}
STAGES = {"pre-seed", "seed", "any"}
SOURCE_TYPES = {"api", "scrape", "llm", "curated"}
CONFIDENCES = {"high", "medium", "low"}

# --- Record shape -----------------------------------------------------------

# The 16 keys, in the exact order they must appear in deadlines.json.
RECORD_FIELDS = [
    "slug",
    "name",
    "category",
    "organizer",
    "deadline",
    "rolling",
    "event_date",
    "location",
    "funding",
    "stage",
    "apply_url",
    "source_url",
    "source_type",
    "last_verified",
    "confidence",
    "needs_review",
]

# Fields that must be present and non-empty for a record to be considered usable.
REQUIRED_NONEMPTY = ["name", "category", "organizer", "apply_url", "source_url"]


def empty_record() -> dict:
    """Return a record dict with every key present and sensibly defaulted.

    Strings default to ``""``; ``deadline``/``event_date`` to ``None``;
    ``rolling``/``needs_review`` to ``False``; ``confidence`` to ``"low"``;
    ``source_type`` to ``"scrape"``. Keys are inserted in :data:`RECORD_FIELDS`
    order so the resulting JSON is stable.
    """
    rec: dict = {}
    for field in RECORD_FIELDS:
        if field in ("deadline", "event_date"):
            rec[field] = None
        elif field in ("rolling", "needs_review"):
            rec[field] = False
        elif field == "confidence":
            rec[field] = "low"
        elif field == "source_type":
            rec[field] = "scrape"
        else:
            rec[field] = ""
    return rec


# --- Gemini structured-output schema ----------------------------------------

# A plain dict JSON Schema describing {"records": [ <record>, ... ]}.
# The google-genai SDK accepts a plain dict for ``response_schema`` (it is
# coerced into ``types.Schema``), which keeps this portable across SDK
# versions. The LLM is asked to return an object with a single "records" array.
GEMINI_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "records": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "category": {
                        "type": "STRING",
                        "enum": sorted(CATEGORIES),
                    },
                    "organizer": {"type": "STRING"},
                    "deadline": {
                        "type": "STRING",
                        "description": (
                            "Application deadline as an ISO 8601 date or "
                            "datetime, or empty if rolling/unknown."
                        ),
                    },
                    "rolling": {"type": "BOOLEAN"},
                    "event_date": {
                        "type": "STRING",
                        "description": "Event/cohort start date (ISO) or empty.",
                    },
                    "location": {"type": "STRING"},
                    "funding": {
                        "type": "STRING",
                        "description": (
                            "Funding amount/terms, e.g. '$500,000' or "
                            "'Equity-free'."
                        ),
                    },
                    "stage": {
                        "type": "STRING",
                        "enum": sorted(STAGES),
                    },
                    "apply_url": {"type": "STRING"},
                },
                # Gemini honours ``required`` and ``property_ordering``.
                "required": ["name", "category", "organizer"],
                "property_ordering": [
                    "name",
                    "category",
                    "organizer",
                    "deadline",
                    "rolling",
                    "event_date",
                    "location",
                    "funding",
                    "stage",
                    "apply_url",
                ],
            },
        }
    },
    "required": ["records"],
}
