/**
 * The single shared data contract.
 *
 * `src/data/deadlines.json` is a JSON array of objects with EXACTLY these 16
 * keys, in this order. Frontend code must read ONLY these fields.
 */

/** Funding program category. */
export type Category = "accelerator" | "competition" | "grant" | "fellowship";

/** Target company stage. */
export type Stage = "pre-seed" | "seed" | "any";

/** How a record was produced (provenance). */
export type SourceType = "api" | "scrape" | "llm" | "curated";

/** Confidence in the record's accuracy. */
export type Confidence = "high" | "medium" | "low";

export interface Deadline {
  /** Stable key: name+cycle lowercased kebab-case, e.g. "yc-fall-2026". */
  slug: string;
  /** Display name, e.g. "Y Combinator — Fall 2026". */
  name: string;
  /** One of: accelerator | competition | grant | fellowship. */
  category: Category;
  /** Organizing body, e.g. "Y Combinator". */
  organizer: string;
  /** ISO 8601 UTC like "2026-08-12T03:00:00Z", or null if rolling. */
  deadline: string | null;
  /** True when applications are accepted on a rolling basis. */
  rolling: boolean;
  /** ISO date "2026-10-01" or null. */
  event_date: string | null;
  /** "San Francisco, CA" or "Remote". */
  location: string;
  /** Human string like "$500,000", "$100,000", or "Equity-free". */
  funding: string;
  /** One of: pre-seed | seed | any. */
  stage: Stage;
  /** https URL to apply. */
  apply_url: string;
  /** https URL of the source. */
  source_url: string;
  /** One of: api | scrape | llm | curated. */
  source_type: SourceType;
  /** ISO 8601 UTC timestamp like "2026-06-06T00:00:00Z". */
  last_verified: string;
  /** One of: high | medium | low. */
  confidence: Confidence;
  /** True when a human should double-check this record. */
  needs_review: boolean;
}
