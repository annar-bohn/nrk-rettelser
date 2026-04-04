# NRK Rettelser

Unofficial tool that automatically collects, classifies, and displays corrections from NRK's online journalism. Inspired by [vg.no/rettelser](https://www.vg.no/rettelser), but for NRK.

- **Live site:** https://annar-bohn.github.io/nrk-rettelser/
- **Repo:** https://github.com/annar-bohn/nrk-rettelser
- **Owner:** Annar Bohn
- **Local path:** `/Users/annarbohn/Code/nrk-rettelser`

---

## Architecture Overview

```
scraper.py  ──→  data/corrections_raw.json  ──→  enrich_qa.py  ──→  data/corrections.json  ──→  index.html
(search + sitemap)       (all entries, qa_status)        (Gemini AI)         (genuine + pending only)       (frontend)
```

### Pipeline

1. **scraper.py** — Finds corrections via two methods:
   - **Search scan** (~30 sec): Queries NRK search for 10+ correction trigger phrases, checks first 2 pages per term
   - **Sitemap scan** (~5–15 min): Checks articles modified in last 30 days, up to 1000 URLs across 50 sub-sitemaps
   - Writes new entries to `data/corrections_raw.json` with `qa_status: "pending"`

2. **enrich_qa.py** — AI enrichment + quality assurance:
   - Fetches each pending article's full HTML
   - Calls **Gemini 3.1 Flash Lite** (`gemini-3.1-flash-lite-preview`) to classify and enrich
   - Outputs: `qa_status`, `correction_description`, `correction_date`, `news_category`, `correction_type`, `journalist`, `responsible_editor`
   - Writes filtered output (genuine + pending + uncertain, NOT false_positive/not_a_correction) to `data/corrections.json`

3. **Frontend** — `index.html` loads `corrections.json` and renders a searchable, filterable list

### Scheduled Workflows (GitHub Actions)

| Workflow | Schedule | What it does |
|----------|----------|-------------|
| **Scrape + Enrich** (`update.yml`) | Every 6 hours | Search scan → sitemap scan → enrich-qa |
| **Deep sitemap crawl** (`backfill_sitemap.yml`) | Daily at 03:17 + manual | Full sitemap crawl, 90-day lookback, 240-min time budget, resumes via sitemap_progress.json |
| **Backfill 2** (`backfill2.yml`) | Manual only | Search-based backfill for specific terms, then enrich |

All workflows share `concurrency: group: nrk-rettelser` to prevent simultaneous runs.

---

## Key Files

| File | Purpose |
|------|---------|
| `index.html` | Main frontend — NRK-inspired design, watchdog logo, search, filters (category, type), card display with badges, byline, rettetid |
| `stats.html` | Statistics page — 3 Chart.js charts (corrections/month, avg response time, error type doughnut with filters) |
| `metode.html` | Methodology page — describes data sources, triggers, AI enrichment, limitations |
| `scraper.py` | Search-based + sitemap scanner, writes to `corrections_raw.json` |
| `enrich_qa.py` | Gemini AI enrichment and QA classification |
| `backfill.py` | Original one-shot backfill (search-based, 7 terms, already run — found 1068 corrections) |
| `backfill2.py` | Second-pass backfill for 3 additional terms: `retting:`, `presisering:`, `etter publisering` |
| `backfill_sitemap.py` | Deep sitemap crawl with resume support (`data/sitemap_progress.json`) |
| `migrate_to_raw.py` | One-time migration script: `corrections.json` → `corrections_raw.json` (already run) |
| `data/corrections_raw.json` | All entries with full qa_status field |
| `data/corrections.json` | Frontend-filtered entries (excludes `not_a_correction` and `false_positive`) |

---

## Correction Detection

### Trigger Phrases (Bokmål + Nynorsk)

**Bokmål:**
`i en tidligere versjon`, `i en eldre versjon`, `i en tidligere publisert versjon`, `nrk retter`, `nrk har rettet`, `nrk korrigerer`, `nrk beklager`, `rettelse:`, `korrigering:`, `endringen er gjort`, `vi har rettet`, `artikkelen er oppdatert`, `tidligere skrev vi`

**Nynorsk:**
`retting`, `retting:`, `presisering:`, `etter publisering`, `endringane vart gjort`, `endringane er gjort`, `det er gjort endringar`, `artikkelen er endra`

### Three-Pass Extraction Strategy

1. **Pass 1:** `<p>` tags (≤2000 chars) — highest quality
2. **Pass 2:** `<aside>` / `<blockquote>` (≤2000 chars) — catches fact-box corrections
3. **Pass 3:** Leaf `<div>` elements (≤400 chars, no child p/div) — last resort

**NAV_NOISE filter:** Skips elements starting with "hopp til innhold", "nrk tv", "nrk radio", "nrk super", "nrk p3"

### Limits

- Article body passed to Gemini: **20,000 chars** (covers virtually all articles fully)
- Correction text field: **2,000 chars**
- `<aside>` extraction limit: **2,000 chars** (was 500, raised to catch fact-box corrections)

---

## AI Enrichment Details

### Model
**Gemini 3.1 Flash Lite** (`gemini-3.1-flash-lite-preview`) via Google AI Studio free tier

- **500 RPD** (requests per day), **15 RPM**, 250K TPM — RPM is the practical bottleneck
- API endpoint: `https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite-preview:generateContent`
- API key stored as GitHub Secret: `GEMINI_API_KEY`

### Model History & Rationale
- Started with Gemini 2.0 Flash → **quota exhausted** (limit: 0 on free tier)
- Tested Gemini 2.5 Flash → works but only 20 RPD (too slow for backlog)
- Tested Gemma 4 26B → good reasoning but outputs chain-of-thought instead of clean JSON
- **Winner: Gemini 3.1 Flash Lite** → 500 RPD, clean JSON output, correct Norwegian classification

### Prompt Output Schema
```json
{
  "qa_status": "genuine_correction | uncertain | not_a_correction",
  "correction_description": "max 200 chars, Norwegian",
  "correction_date": "YYYY-MM-DD | null",
  "news_category": "crime | politics | economy | sports | culture | science | health | environment | technology | international | entertainment | local | other",
  "correction_type": "factual_error | wrong_name | wrong_number | wrong_image | wrong_date | wrong_location | mistranslation | misleading_title | missing_context | source_error | retracted_claim | spelling_grammar | attribution_error | other",
  "journalist": "name",
  "responsible_editor": "name"
}
```

### Frontend Display
Each card shows (when enriched):
- **Category badge** (blue) — e.g. "Krim", "Sport"
- **Correction type badge** (gray) — e.g. "Faktafeil", "Feil tall"
- **NRK section label** (uppercase) — e.g. "NYHETER", "URIX"
- **AI description** (italic) — correction summary
- **Journalist byline**
- **Rettetid** — time-to-correct formatted as "X timer" or "X dager"
- **Dropdown filters** for category and type

Un-enriched entries gracefully show only original fields.

---

## The USAID Article (Critical Test Case)

**URL:** `https://www.nrk.no/urix/xl/hiv-smitta-mary-fryktar-usaid-frysen-vil-ta-liv-1.17271168`

This is the key test article for verifying scraper coverage. It was repeatedly discussed across sessions and the assistant kept forgetting about it. **Do not lose this reference.**

### Why it was missed originally:
1. **XL article format** — uses `/xl/` URL path, different DOM structure
2. **Nynorsk correction markers** — uses `RETTING` (standalone heading, no colon) and `endringane vart gjort` instead of standard Bokmål markers
3. **Correction in `<aside class="fact">` box** — 1378 chars, exceeded old 500-char `<aside>` limit
4. **NRK search doesn't index fact-box content** — so search-based backfills can't find it
5. **403 access issues** — NRK sometimes blocks automated requests

### How it's now handled:
- Nynorsk triggers added (`retting`, `endringane vart gjort`, `det er gjort endringar`, `artikkelen er endra`)
- `<aside>` limit raised from 500 → 2000 chars
- Deep sitemap crawl (`backfill_sitemap.py`) checks all articles, not just search results
- The article's triggers DO match the updated scraper when the HTML is fetched directly

### Current status (as of 2026-04-04):
Not yet found. The daily sitemap crawl should find it organically over the coming days as it works through sitemaps. Check:
```bash
grep -i "usaid\|hiv-smitta" data/corrections_raw.json
```

---

## Forsvarssatsing Article (Second Test Case)

**URL:** `https://www.nrk.no/tromsogfinnmark/flere-partier-forventer-solid-satsing-pa-forsvaret-i-revidert-nasjonalbudsjett-1.17416812`

Another article to verify that the scraper picks up corrections from the newly added `/tromsogfinnmark/` section. This section was missing from `ARTICLE_SECTIONS` until 2026-04-04.

```bash
grep -i "forsvaret.*nasjonalbudsjett\|17416812" data/corrections_raw.json
```

---

## Design

- **Font:** "NRK Sans Variable" with system-ui fallback
- **Accent color:** `#1550c3` (R:21, G:80, B:195) — used for logo, card borders, links
- **Logo:** Watchdog SVG (with sunglasses), inline in HTML, `viewBox="340 120 410 445"`, height 9.6rem, fill `#1550c3`
- **Favicons:** watchdog logo PNGs (16x16, 32x32, apple-touch-icon 180x180, android-chrome 192/512), favicon.ico, site.webmanifest — all pages
- **Tagline:** "Som vg.no/rettelser, bare for NRK"
- **Layout:** White cards on off-white (#f0f0ee) background, red-to-blue left border accent
- **Footer:** Correction count left, "Eksporter JSON" discrete text link right
- **No GitHub URL** shown on the page
- **Search placeholder:** "Søk i listen"
- Nav links: Statistikk → `stats.html`, Metode → `metode.html`

---

## NRK Technical Details

- **RSS feeds:** toppsaker.rss, nyheter/siste.rss, sport/siste.rss, kultur/siste.rss, livsstil/siste.rss (urix 404s)
- **Sitemap index:** `https://www.nrk.no/sitemap.xml` — 500+ sub-sitemaps, `<lastmod>` timestamps
- **NRK search:** `https://www.nrk.no/sok/?q="term"&scope=nrkno&from=N` — offset pagination (20/page), server-rendered HTML, no JSON API. "Neste side" link for has_next.
- **Article sections (ARTICLE_SECTIONS):** `/nyheter/`, `/sport/`, `/kultur/`, `/urix/`, `/norge/`, `/livsstil/`, `/sapmi/`, `/mr/`, `/innlandet/`, `/vestland/`, `/rogaland/`, `/trondelag/`, `/nordland/`, `/sorlandet/`, `/tromsogfinnmark/`, `/vestfoldogtelemark/`, `/vestfold/`, `/osloogviken/`, `/ostlandssendingen/`, `/viten/`, `/dokumentar/`, `/klima/` + legacy regions. Defined in 4 files: `scraper.py`, `backfill_sitemap.py`, `backfill.py`, `backfill2.py` — keep them in sync!
- RSS feeds show **new articles only**, not recently corrected ones → RSS alone catches very few corrections

---

## Development Environment

- **Local path:** `/Users/annarbohn/Code/nrk-rettelser`
- **Claude Code CLI:** Installed at `~/.local/bin/claude` — requires `export PATH="$HOME/.local/bin:$PATH"` in `~/.zshrc`
- **Git auth:** Set up via `gh auth login` (GitHub CLI)
- **GitHub CLI (gh):** Installed via Homebrew, may need PATH refresh in new shell sessions
- **Python deps:** `pip3 install feedparser requests beautifulsoup4`
- **GEMINI_API_KEY:** Set as GitHub Secret + export locally for testing (`export GEMINI_API_KEY="..."`)

---

## Data Schema

### corrections_raw.json entry
```json
{
  "url": "https://www.nrk.no/...",
  "title": "Article headline (legacy field)",
  "headline": "Article headline",
  "date": "ISO date string",
  "correction": "Raw correction text (legacy field)",
  "correction_text_raw": "Raw correction text",
  "source": "rss | sitemap | search_backfill | search_backfill2",
  "qa_status": "pending | genuine_correction | uncertain | not_a_correction | false_positive",
  "correction_description": "AI-generated summary",
  "correction_date": "YYYY-MM-DD",
  "news_category": "crime | politics | ...",
  "correction_type": "factual_error | wrong_name | ...",
  "journalist": "Name",
  "responsible_editor": "Name",
  "nrk_section": "nyheter | sport | ...",
  "publication_date": "ISO date",
  "modified_date": "ISO date",
  "time_to_correct_hours": 48.5,
  "id": "unix_timestamp_ms",
  "auto": true
}
```

### Frontend filtering
`corrections.json` includes entries with `qa_status` in: `pending`, `genuine_correction`, `uncertain`
Excludes: `not_a_correction`, `false_positive`

---

## Known Issues & Gotchas

1. **NRK 403 blocks:** NRK sometimes blocks automated requests. Scripts use browser-like User-Agent headers to mitigate.
2. **RSS doesn't surface edited articles:** Corrections are added to old articles that have dropped off the feed. The search-based approach is far more effective.
3. **NRK search doesn't index `<aside>` fact-box content:** Articles with corrections only inside fact boxes won't appear in search results. Only sitemap crawls can find these.
4. **GitHub Actions 6-hour timeout:** Deep sitemap crawls used to exceed this. Now mitigated by `--max-minutes 240` time budget — script exits cleanly and resumes via `sitemap_progress.json` on next daily run.
5. **Gemini rate limits:** 15 RPM is the practical bottleneck, not 500 RPD. `enrich_qa.py` uses 4-second sleep between requests and retries up to 3× on 429 with 60/120/180s backoff. Typical throughput: ~100-200 per run.
6. **Enrichment processes newest-first:** Pending entries sorted by date descending, capped at `--max-entries 450`. This ensures top-of-page articles get enriched before old backlog.
7. **Time-to-correct uses `modified_date`:** Gemini's `correction_date` guess was often wrong (defaulting to scrape date). Now `time_to_correct_hours` is calculated from `publication_date` → `modified_date` (NRK metadata), with `correction_date` as fallback. Recalculated for all entries on every enrich run.
8. **Old entries with placeholder text:** Some early entries have `"Rettelsestekst ikke tilgjengelig (opprettet av gammel versjon av scraperen – klikk lenken for detaljer)."` — these are from the original scraper before extraction was improved.
9. **Node.js 20 deprecation warning:** `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true` is set but GitHub still shows warnings until action authors update their manifests.
10. **`feedparser` needed for scraper but NOT for backfill scripts** — backfills only use `requests` + `beautifulsoup4`.
11. **Concurrency group `nrk-rettelser`** prevents workflows from running simultaneously and corrupting data files.

---

## Session History Summary

### What has been built (chronological):
1. Basic scraper with RSS feed scanning
2. GitHub Actions workflow for automated runs
3. Frontend (`index.html`) with search and card display
4. Backfill 1 (`backfill.py`) — search-based, found 1068 corrections
5. Frontend redesign — NRK-inspired, watchdog logo, filters, badges
6. Enrichment pipeline (`enrich_qa.py`) with Gemini AI
7. Stats page (`stats.html`) with Chart.js charts
8. Methodology page (`metode.html`)
9. Backfill 2 (`backfill2.py`) — additional search terms, found 169 more
10. Nynorsk trigger expansion + limit increases
11. Scraper rewrite — search-based + improved sitemap scanning
12. Deep sitemap crawl (`backfill_sitemap.py`) for comprehensive historical coverage
13. Model migration: Gemini 2.0 Flash → 3.1 Flash Lite (500 RPD, clean JSON)
14. Git/GitHub CLI setup for local development
15. **[2026-04-04]** Daily sitemap backfill with 240-min time budget (was weekly, timed out at 6h)
16. **[2026-04-04]** Enrichment: newest-first sorting + `--max-entries 450` cap
17. **[2026-04-04]** Fixed time-to-correct: use `modified_date` instead of Gemini's `correction_date` guess
18. **[2026-04-04]** Added 9 missing NRK sections: `/vestfoldogtelemark/`, `/tromsogfinnmark/`, `/viten/`, `/klima/`, etc.
19. **[2026-04-04]** Gemini 429 retry with backoff (60/120/180s) + 4s inter-request sleep
20. **[2026-04-04]** Favicons added (watchdog with sunglasses) across all pages + web manifest
21. **[2026-04-04]** Comprehensive CLAUDE.md rewrite with full project context and test cases

### Recurring pain points to avoid:
- **Always remember the USAID article** — it's the canonical test case for scraper coverage
- **Also check the Forsvarssatsing article** — second test case for `/tromsogfinnmark/` coverage
- **Check `qa_status` distribution** before assuming enrichment is working: `grep -c '"qa_status"' data/corrections_raw.json`
- **Don't assume RSS catches corrections** — it doesn't, use search-based approach
- **Test Gemini model changes locally first** with `test_enrich.py` pattern (3 articles: 2 genuine, 1 not-a-correction)
- **ARTICLE_SECTIONS is defined in 4 files** — keep `scraper.py`, `backfill_sitemap.py`, `backfill.py`, `backfill2.py` in sync when adding sections
- **gh CLI needs PATH** — use `export PATH="/opt/homebrew/bin:$PATH"` in bash commands

### Open questions / things to monitor:
1. **USAID article not yet found** — daily backfill should pick it up as it works through sitemaps. Monitor over coming days.
2. **Forsvarssatsing article not yet found** — same, depends on backfill reaching `/tromsogfinnmark/` sitemaps.
3. **Enrichment throughput** — with retry logic and 4s sleep, expect ~100-200/run. As of 2026-04-04: 1644 pending, 69 genuine. Should clear backlog in ~3-4 days.
4. **Raw extraction quality** — correction_text_raw grabs entire `<p>` elements including article body text. Not a problem since Gemini's `correction_description` provides clean summaries, but could be improved with smarter text segmentation in the future.
5. **`modified_date` as correction date** — more accurate than Gemini's guess for time-to-correct, but `modified_date` could reflect non-correction edits. Acceptable trade-off for now.
