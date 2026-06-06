// @ts-check
import { defineConfig } from "astro/config";

// FundingDeadlines is a fully static site: every page is rendered at build
// time from src/data/deadlines.json. No SSR adapter is needed.
export default defineConfig({
  output: "static",
  // Update `site` to your deployed origin (used for canonical URLs / sitemaps).
  site: "https://example.com",
  compressHTML: true,
});
