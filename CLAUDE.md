# NRK Rettelser – prosjektoversikt for Claude

## Hva er dette?
Et uoffisielt verktøy som automatisk samler og viser rettelser fra NRKs nettjournalistikk.
Inspirert av vg.no/rettelser. Publisert via GitHub Pages.

**GitHub-repo:** https://github.com/annar-bohn/nrk-rettelser
**Live side:** https://annar-bohn.github.io/nrk-rettelser/

## Nøkkelfiler

| Fil | Hva den gjør |
|-----|--------------|
| `scraper.py` | Søkebasert skanning (NRK-søk + sitemap). Kjøres hvert 6. time. |
| `enrich_qa.py` | Bruker Gemini 3.1 Flash Lite til å QA-sjekke og berike rådata |
| `backfill_sitemap.py` | Dyp sitemap-crawl for å finne rettelser i faktabokser m.m. |
| `backfill2.py` | Historisk bakover-skanning via NRK-søk (Nynorsk-termer lagt til) |
| `cleanup_and_add.py` | Fjerner placeholder-oppføringer fra corrections_raw.json |
| `migrate_to_raw.py` | Engangsmigrering fra gammelt `corrections.json`-format |
| `backfill.py` | Første bakover-skanning (eldre artikler, historisk) |
| `index.html` | Forsiden – laster `data/corrections.json` og viser søkbar liste |
| `stats.html` | Statistikkside med grafer og nøkkeltall |
| `metode.html` | Metodeside som forklarer hvordan innsamlingen fungerer |

## Data-pipeline

```
scraper.py → corrections_raw.json → enrich_qa.py → corrections.json → index.html
```

- `corrections_raw.json`: rådata fra skraperen (alle funn, ikke QA-sjekket)
- `corrections.json`: ferdig QA-sjekket data som vises på siden

## Skraperarkitektur (scraper.py)

**Søkebasert skanning** (~30 sek): Søker NRK for 10 rettelse-fraser, sjekker de 2 første sidene per søketerm. Fanger de fleste nye rettelser raskt.

**Sitemap-skanning** (~5–15 min): Sjekker artikler endret siste 30 dager, opptil 1000 URL-er fra 50 under-sitemaps. Fanger faktaboks-rettelser som NRK-søk ikke indekserer.

### Trigger-fraser (Bokmål + Nynorsk)
Alle triggere finnes i `TRIGGERS`-listen i scraper.py. Inkluderer:
- Bokmål: "rettelse:", "i en tidligere versjon", "nrk beklager", "vi har rettet" osv.
- Nynorsk: "retting", "endringane vart gjort", "det er gjort endringar", "artikkelen er endra" osv.

### Korreksjonsblokkutvinning
Tre-pass: `<p>` (≤800 tegn) → `<aside>`/`<blockquote>` (≤2000 tegn) → `<div>` (≤800 tegn).
Maks lengde på lagret rettelsestekst: 2000 tegn.

## Berikelse (enrich_qa.py)

**Modell:** `gemini-3.1-flash-lite-preview` (500 RPD gratis)
- Tidligere: gemini-2.0-flash (gratisnivå kvote ble 0, dødt)
- Gemma 4 26B ble testet men ga ikke ren JSON-output

**Prompt:** Norsk, med fullt spesifiserte kategorier:
- `qa_status`: genuine_correction, uncertain, not_a_correction
- `news_category`: 13 kategorier (sports, culture, politics, economy, science, health, technology, local, world, crime, weather, entertainment, other)
- `correction_type`: 14 typer (factual_error, wrong_name, wrong_number, wrong_image, wrong_date, wrong_location, mistranslation, misleading_title, missing_context, source_error, retracted_claim, spelling_grammar, attribution_error, other)

**Artikkelkropp:** Opptil 20 000 tegn sendes til modellen (tidligere 2000).
**Rettelsestekst:** Opptil 2000 tegn (tidligere 700).

Oppføringer med `not_a_correction` filtreres ut fra frontend via `INCLUDE_STATUSES`.

## GitHub Actions workflows

| Workflow | Fil | Trigger | Hva den gjør |
|----------|-----|---------|--------------|
| Scrape and Enrich | `update.yml` | Hvert 6. time + manuelt | Søkebasert skraping → enrich_qa |
| Sitemap backfill | `backfill_sitemap.yml` | Ukentlig (søndager) + manuelt | Dyp sitemap-crawl → enrich_qa |
| Backfill 2 | `backfill2.yml` | Manuelt | NRK-søk backfill → enrich_qa |

Alle workflows bruker concurrency group `nrk-rettelser` med `cancel-in-progress: false`
så de ikke kjører samtidig og krasjer med push-konflikter.

## Secrets
- `GEMINI_API_KEY` – satt i GitHub repo settings, brukes av `enrich_qa.py`
  - Google AI Studio prosjekt: `gen-lang-client-0714683784`
  - Gratisnivå: 500 RPD for gemini-3.1-flash-lite-preview
  - Sjekk kvote: https://ai.dev/rate-limit

## Tekniske detaljer
- GitHub Actions bruker `stefanzweifel/git-auto-commit-action@v5`
- Krever env: `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true`
- Alle commit-jobber bruker `ref: main` på checkout
- `gh` CLI er installert lokalt via Homebrew, autentisert med `gh auth login`
- Git push over HTTPS fungerer (autentisert via gh)

## Arbeidsflyt
Rediger filer lokalt i `/Users/annarbohn/Code/nrk-rettelser/`, commit og push:
```bash
cd /Users/annarbohn/Code/nrk-rettelser
git add <filer>
git commit -m "beskrivelse"
git push
```

Trigger workflows manuelt:
```bash
gh workflow run "Scrape and Enrich NRK Rettelser"
gh workflow run "Backfill – full sitemap crawl" -f days=90
```

Sjekk workflow-logger:
```bash
gh run list --limit 5
gh run view <run-id> --log | grep enrich-qa | tail -20
```
