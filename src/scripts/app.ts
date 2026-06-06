/**
 * Client-side interactivity for FundingDeadlines.
 *
 * Astro renders every card at build time; this script *enhances* the existing
 * DOM — it never re-renders cards. It owns:
 *   - category tab filtering
 *   - text search (name / organizer / location)
 *   - live re-sort (soonest / latest / name)
 *   - per-second countdown ticks (urgent <7d gets stronger weight)
 *   - per-card AND per-category .ics export (built here, downloaded via Blob)
 *   - "report wrong date" prefilled GitHub issue links
 *   - dark/light theme toggle (persisted to localStorage)
 *
 * All program data is read from data-* attributes on the cards, so there is a
 * single source of truth (the build-time render).
 */

interface FdConfig {
  githubRepo: string;
  siteTitle: string;
  featuredSlug: string;
}

type SortMode = "soonest" | "latest" | "name";

const MS_PER_DAY = 86_400_000;
const URGENT_MS = 7 * MS_PER_DAY;
const FAR_FUTURE = Number.MAX_SAFE_INTEGER;

/** Read a required element or throw — keeps the rest of the code non-null. */
function must<T extends Element>(selector: string, root: ParentNode = document): T {
  const el = root.querySelector<T>(selector);
  if (!el) throw new Error(`FundingDeadlines: missing element "${selector}"`);
  return el;
}

/** Parse the JSON config block emitted by index.astro. */
function readConfig(): FdConfig {
  const node = document.getElementById("fd-config");
  const fallback: FdConfig = {
    githubRepo: "your-org/funding-deadlines",
    siteTitle: "FundingDeadlines",
    featuredSlug: "",
  };
  if (!node || !node.textContent) return fallback;
  try {
    return { ...fallback, ...(JSON.parse(node.textContent) as Partial<FdConfig>) };
  } catch {
    return fallback;
  }
}

/* -------------------------------------------------------------------------- */
/* Per-card model (read once from the DOM)                                    */
/* -------------------------------------------------------------------------- */

interface CardModel {
  el: HTMLElement;
  slug: string;
  category: string;
  search: string;
  sortKey: number;
  name: string;
  organizer: string;
  location: string;
  funding: string;
  deadline: string; // "" when rolling / unknown
  eventDate: string; // "" when none
  applyUrl: string;
  rolling: boolean;
  countdownEl: HTMLElement | null;
  countdownValueEl: HTMLElement | null;
}

function getAttr(el: HTMLElement, name: string): string {
  return el.getAttribute(name) ?? "";
}

function collectCards(): CardModel[] {
  const nodes = document.querySelectorAll<HTMLElement>("[data-card]");
  const cards: CardModel[] = [];
  nodes.forEach((el) => {
    const countdownEl = el.querySelector<HTMLElement>("[data-countdown]");
    cards.push({
      el,
      slug: getAttr(el, "data-slug"),
      category: getAttr(el, "data-category"),
      search: getAttr(el, "data-search"),
      sortKey: Number(getAttr(el, "data-sort-key")) || FAR_FUTURE,
      name: getAttr(el, "data-name"),
      organizer: getAttr(el, "data-organizer"),
      location: getAttr(el, "data-location"),
      funding: getAttr(el, "data-funding"),
      deadline: getAttr(el, "data-deadline"),
      eventDate: getAttr(el, "data-event-date"),
      applyUrl: getAttr(el, "data-apply-url"),
      rolling: getAttr(el, "data-rolling") === "true",
      countdownEl,
      countdownValueEl:
        countdownEl?.querySelector<HTMLElement>("[data-countdown-value]") ?? null,
    });
  });
  return cards;
}

/* -------------------------------------------------------------------------- */
/* Countdown                                                                  */
/* -------------------------------------------------------------------------- */

function formatCountdown(card: CardModel, now: number): void {
  const valueEl = card.countdownValueEl;
  const wrapEl = card.countdownEl;
  if (!valueEl || !wrapEl) return;

  // Reset state classes each tick; re-add the one that applies.
  wrapEl.classList.remove("is-urgent", "is-rolling", "is-closed");

  if (card.rolling || !card.deadline) {
    valueEl.textContent = "Rolling — apply anytime";
    wrapEl.classList.add("is-rolling");
    return;
  }

  const target = Date.parse(card.deadline);
  if (Number.isNaN(target)) {
    valueEl.textContent = "Date unavailable";
    wrapEl.classList.add("is-closed");
    return;
  }

  const diff = target - now;
  if (diff <= 0) {
    valueEl.textContent = "Closed";
    wrapEl.classList.add("is-closed");
    return;
  }

  const totalSeconds = Math.floor(diff / 1000);
  const days = Math.floor(totalSeconds / 86_400);
  const hours = Math.floor((totalSeconds % 86_400) / 3_600);
  const minutes = Math.floor((totalSeconds % 3_600) / 60);
  const seconds = totalSeconds % 60;

  valueEl.textContent = `${days}d ${hours}h ${minutes}m ${seconds}s`;
  if (diff < URGENT_MS) wrapEl.classList.add("is-urgent");
}

function startCountdowns(cards: CardModel[]): void {
  const tick = (): void => {
    const now = Date.now();
    for (const card of cards) formatCountdown(card, now);
  };
  tick();
  window.setInterval(tick, 1000);
}

/* -------------------------------------------------------------------------- */
/* Filter + search + sort                                                     */
/* -------------------------------------------------------------------------- */

interface ViewState {
  category: string;
  query: string;
  sort: SortMode;
}

function liveSortKey(card: CardModel, now: number): number {
  if (card.rolling || !card.deadline) return FAR_FUTURE;
  const t = Date.parse(card.deadline);
  if (Number.isNaN(t)) return FAR_FUTURE;
  if (t < now) return FAR_FUTURE - 1; // closed sorts after upcoming
  return t;
}

function applyView(
  cards: CardModel[],
  state: ViewState,
  grid: HTMLElement,
  resultsMeta: HTMLElement,
  emptyEl: HTMLElement,
  featuredSlug: string,
): void {
  const q = state.query.trim().toLowerCase();
  const now = Date.now();

  // 1) Determine which cards are visible.
  const visible: CardModel[] = [];
  for (const card of cards) {
    const matchesCategory =
      state.category === "all" || card.category === state.category;
    const matchesQuery = q === "" || card.search.includes(q);
    const show = matchesCategory && matchesQuery;
    card.el.hidden = !show;
    if (show) visible.push(card);
  }

  // 2) Sort the visible cards and reorder them in the DOM.
  const collator = new Intl.Collator("en", { sensitivity: "base" });
  visible.sort((a, b) => {
    switch (state.sort) {
      case "name":
        return collator.compare(a.name, b.name);
      case "latest": {
        // Latest first, but rolling/closed still sink to the bottom.
        const ka = liveSortKey(a, now);
        const kb = liveSortKey(b, now);
        const aSpecial = ka >= FAR_FUTURE - 1;
        const bSpecial = kb >= FAR_FUTURE - 1;
        if (aSpecial && bSpecial) return ka - kb;
        if (aSpecial) return 1;
        if (bSpecial) return -1;
        return kb - ka;
      }
      case "soonest":
      default:
        return liveSortKey(a, now) - liveSortKey(b, now);
    }
  });
  for (const card of visible) grid.appendChild(card.el);

  // 3) Highlight the single nearest upcoming deadline *within the visible set*.
  //    Falls back to the build-time featuredSlug when nothing is upcoming.
  let featured: CardModel | null = null;
  for (const card of visible) {
    if (card.rolling || !card.deadline) continue;
    const t = Date.parse(card.deadline);
    if (Number.isNaN(t) || t <= now) continue;
    if (!featured || t < Date.parse(featured.deadline)) featured = card;
  }
  for (const card of cards) {
    const isFeatured = featured
      ? card === featured
      : card.slug === featuredSlug && !card.el.hidden;
    card.el.classList.toggle("is-featured", isFeatured);
  }

  // 4) Update meta + empty state.
  resultsMeta.textContent = `Showing ${visible.length} of ${cards.length} programs`;
  emptyEl.hidden = visible.length !== 0;
}

/* -------------------------------------------------------------------------- */
/* ICS export                                                                 */
/* -------------------------------------------------------------------------- */

/** Escape per RFC 5545 (text values): backslash, semicolon, comma, newline. */
function icsEscape(text: string): string {
  return text
    .replace(/\\/g, "\\\\")
    .replace(/;/g, "\\;")
    .replace(/,/g, "\\,")
    .replace(/\r?\n/g, "\\n");
}

/** Format a Date as a UTC iCal timestamp: 20260812T030000Z. */
function toIcsUtc(d: Date): string {
  const p = (n: number, w = 2): string => String(n).padStart(w, "0");
  return (
    `${d.getUTCFullYear()}${p(d.getUTCMonth() + 1)}${p(d.getUTCDate())}T` +
    `${p(d.getUTCHours())}${p(d.getUTCMinutes())}${p(d.getUTCSeconds())}Z`
  );
}

/** Build a single VEVENT for a card. Returns null for rolling/invalid. */
function buildVevent(card: CardModel, dtstamp: string): string | null {
  if (card.rolling || !card.deadline) return null;
  const target = new Date(card.deadline);
  if (Number.isNaN(target.getTime())) return null;

  const dtstart = toIcsUtc(target);
  // Give the deadline a 1-hour block so it shows nicely on calendars.
  const dtend = toIcsUtc(new Date(target.getTime() + 3_600_000));

  const descriptionParts = [
    `Organizer: ${card.organizer}`,
    `Funding: ${card.funding}`,
    `Location: ${card.location}`,
    card.eventDate ? `Event date: ${card.eventDate}` : "",
    `Apply: ${card.applyUrl}`,
  ].filter(Boolean);

  return [
    "BEGIN:VEVENT",
    `UID:${card.slug}@funding-deadlines`,
    `DTSTAMP:${dtstamp}`,
    `DTSTART:${dtstart}`,
    `DTEND:${dtend}`,
    `SUMMARY:${icsEscape(`Apply: ${card.name}`)}`,
    `DESCRIPTION:${icsEscape(descriptionParts.join("\n"))}`,
    `LOCATION:${icsEscape(card.location)}`,
    `URL:${card.applyUrl}`,
    "END:VEVENT",
  ].join("\r\n");
}

/** Wrap one or more VEVENT strings into a complete VCALENDAR. */
function buildCalendar(vevents: string[], siteTitle: string): string {
  return [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    `PRODID:-//${siteTitle}//EN`,
    "CALSCALE:GREGORIAN",
    "METHOD:PUBLISH",
    ...vevents,
    "END:VCALENDAR",
  ].join("\r\n");
}

/** Trigger a client-side download of `content` as `filename`. */
function downloadText(content: string, filename: string): void {
  const blob = new Blob([content], { type: "text/calendar;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoke on the next tick so the download has a chance to start.
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

/** Make a safe, kebab-ish filename fragment. */
function safeName(text: string): string {
  return (
    text
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "funding-deadlines"
  );
}

/* -------------------------------------------------------------------------- */
/* Report-wrong-date links                                                    */
/* -------------------------------------------------------------------------- */

function wireReportLinks(cards: CardModel[], config: FdConfig): void {
  for (const card of cards) {
    const link = card.el.querySelector<HTMLAnchorElement>("[data-report-link]");
    if (!link) continue;

    const title = `Wrong date: ${card.name} (${card.slug})`;
    const body = [
      `**Program:** ${card.name}`,
      `**Slug:** \`${card.slug}\``,
      `**Organizer:** ${card.organizer}`,
      "",
      `**Current deadline shown:** ${card.deadline || "(rolling / none)"}`,
      "**Correct deadline (ISO 8601 UTC):** ",
      "",
      "**Source for the correct date:** ",
      "",
      "_Thanks for helping keep FundingDeadlines accurate._",
    ].join("\n");

    const url =
      `https://github.com/${config.githubRepo}/issues/new` +
      `?title=${encodeURIComponent(title)}` +
      `&body=${encodeURIComponent(body)}` +
      `&labels=${encodeURIComponent("data-correction")}`;
    link.href = url;
  }
}

/* -------------------------------------------------------------------------- */
/* Theme toggle                                                               */
/* -------------------------------------------------------------------------- */

function wireThemeToggle(): void {
  const toggle = document.getElementById("theme-toggle");
  if (!toggle) return;
  toggle.addEventListener("click", () => {
    const root = document.documentElement;
    const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try {
      localStorage.setItem("fd-theme", next);
    } catch {
      /* storage may be unavailable (private mode); ignore. */
    }
  });
}

/* -------------------------------------------------------------------------- */
/* Wiring                                                                     */
/* -------------------------------------------------------------------------- */

function init(): void {
  const config = readConfig();
  const cards = collectCards();

  // Always-on features regardless of whether there are cards.
  wireThemeToggle();
  startCountdowns(cards);
  wireReportLinks(cards, config);

  const grid = must<HTMLElement>("#grid");
  const resultsMeta = must<HTMLElement>("#results-meta");
  const emptyEl = must<HTMLElement>("#empty");
  const searchInput = must<HTMLInputElement>("#search");
  const sortSelect = must<HTMLSelectElement>("#sort");
  const exportBtn = must<HTMLButtonElement>("#export-category");
  const tabButtons = Array.from(
    document.querySelectorAll<HTMLButtonElement>("[data-tab]"),
  );

  const state: ViewState = { category: "all", query: "", sort: "soonest" };
  const rerender = (): void =>
    applyView(cards, state, grid, resultsMeta, emptyEl, config.featuredSlug);

  // Tabs.
  for (const btn of tabButtons) {
    btn.addEventListener("click", () => {
      state.category = btn.getAttribute("data-tab") ?? "all";
      for (const b of tabButtons) {
        b.setAttribute("aria-selected", b === btn ? "true" : "false");
      }
      rerender();
    });
  }

  // Search (debounced lightly via requestAnimationFrame).
  let rafId = 0;
  searchInput.addEventListener("input", () => {
    state.query = searchInput.value;
    if (rafId) cancelAnimationFrame(rafId);
    rafId = requestAnimationFrame(rerender);
  });

  // Sort.
  sortSelect.addEventListener("change", () => {
    const v = sortSelect.value;
    state.sort = v === "latest" || v === "name" ? v : "soonest";
    rerender();
  });

  // Per-card .ics export (event-delegated).
  const dtstamp = toIcsUtc(new Date());
  grid.addEventListener("click", (event) => {
    const trigger = (event.target as HTMLElement).closest<HTMLElement>(
      "[data-ics-card]",
    );
    if (!trigger) return;
    const cardEl = trigger.closest<HTMLElement>("[data-card]");
    if (!cardEl) return;
    const card = cards.find((c) => c.el === cardEl);
    if (!card) return;
    const vevent = buildVevent(card, dtstamp);
    if (!vevent) return;
    downloadText(
      buildCalendar([vevent], config.siteTitle),
      `${safeName(card.name)}.ics`,
    );
  });

  // Per-category (visible set) .ics export.
  exportBtn.addEventListener("click", () => {
    const vevents: string[] = [];
    for (const card of cards) {
      if (card.el.hidden) continue;
      const vevent = buildVevent(card, dtstamp);
      if (vevent) vevents.push(vevent);
    }
    if (vevents.length === 0) {
      exportBtn.classList.add("is-empty");
      window.setTimeout(() => exportBtn.classList.remove("is-empty"), 600);
      return;
    }
    const label = state.category === "all" ? "all" : state.category;
    downloadText(
      buildCalendar(vevents, config.siteTitle),
      `funding-deadlines-${safeName(label)}.ics`,
    );
  });

  // Initial paint.
  rerender();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init, { once: true });
} else {
  init();
}
