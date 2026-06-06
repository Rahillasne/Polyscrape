"""Adapter base classes.

An :class:`Adapter` knows how to ``fetch()`` a raw payload from one source and
``extract()`` a list of partial records from it. The thin subclasses
(:class:`ApiAdapter`, :class:`ScrapeAdapter`, :class:`LlmAdapter`) only exist to
document the kind of source and to set the correct ``source_type`` enum value.
"""

from __future__ import annotations


class Adapter:
    """Base class for all source adapters.

    Subclasses set :attr:`name` and :attr:`source_type` and implement
    :meth:`fetch` and :meth:`extract`. ``extract`` may return *partial*
    records — the run loop fills defaults and runs the validation gate.
    """

    name: str = "base"
    source_type: str = "scrape"

    def fetch(self) -> object:
        """Retrieve the raw payload for this source (JSON, HTML, etc.)."""
        raise NotImplementedError

    def extract(self, raw) -> list[dict]:
        """Turn a raw payload into a list of (partial) record dicts."""
        raise NotImplementedError

    def cache_key(self) -> str:
        """Key used for change-detection caching (defaults to the name)."""
        return self.name


class ApiAdapter(Adapter):
    """An adapter backed by a structured API endpoint."""

    source_type = "api"


class ScrapeAdapter(Adapter):
    """An adapter that scrapes structured data out of HTML."""

    source_type = "scrape"


class LlmAdapter(Adapter):
    """An adapter that uses an LLM to extract records from unstructured text."""

    source_type = "llm"
