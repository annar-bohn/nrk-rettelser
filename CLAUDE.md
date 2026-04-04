# NRK Rettelser – prosjektoversikt for Claude

## Hva er dette?
Et uoffisielt verktøy som automatisk samler og viser rettelser fra NRKs nettjournalistikk.
Inspirert av vg.no/rettelser. Publisert via GitHub Pages.

**GitHub-repo:** https://github.com/annar-bohn/nrk-rettelser
**Live side:** https://annar-bohn.github.io/nrk-rettelser/

## Nøkkelfiler

| Fil | Hva den gjør |
|-----|--------------|
| `scraper.py` | Henter artikler fra NRKs RSS-feeds og sitemap, skriver til `data/corrections_raw.json` |
| `enrich_qa.py` | Bruker Gemini 2.0 Flash til å QA-sjekke og berike rådata, skriver til `data/corrections.json` |
| `migrate_to_raw.py` | Engangsmigrering fra gammelt `corrections.json`-format til `corrections_raw.json` |
| `backfill.py` | Første bakover-skanning (eldre artikler) |
| `backfill2.py` | Andre bakover-skanning – søker etter "retting:", "presisering:", "etter publisering" via NRKs søk |
| `index.html` | Forsiden – laster `data/corrections.json` og viser søkbar liste |
| `stats.html` | Statistikkside med grafer og nøkkeltall |
| `metode.html` | Metodeside som forklarer hvordan innsamlingen fungerer |

## Data-pipeline

```
scraper.py → corrections_raw.json → enrich_qa.py → corrections.json → index.html
```

- `corrections_raw.json`: rådata fra skraperen (alle funn, ikke QA-sjekket)
- `corrections.json`: ferdig QA-sjekket data som vises på siden

## GitHub Actions workflows

| Workflow | Fil | Trigger | Hva den gjør |
|----------|-----|---------|--------------|
| Scrape and Enrich | `.github/workflows/update.yml` | Hvert 6. time + manuelt | Kjører scraper.py → enrich_qa.py |
| Backfill 2 | `.github/workflows/backfill2.yml` | Manuelt | migrate → backfill2 → enrich_qa (én jobb, én push) |

Alle workflows bruker concurrency group `nrk-rettelser` med `cancel-in-progress: false`
så de ikke kjører samtidig og krasjer med push-konflikter.

## Secrets
- `GEMINI_API_KEY` – satt i GitHub repo settings, brukes av `enrich_qa.py`

## Tekniske detaljer
- GitHub Actions bruker `stefanzweifel/git-auto-commit-action@v5`
- Krever env: `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true` for at actionen skal fungere
- Alle commit-jobber bruker `ref: main` på checkout for å alltid jobbe mot siste versjon

## Kjent gjenstående arbeid
- Sette opp `gh` CLI eller SSH-nøkkel for å pushe endringer fra terminalen i stedet for via nettleseren
- `stats.html` er ikke ferdig lastet opp (ble stuck pga filstørrelse i forrige sesjon – sjekk om den er på plass)

## Arbeidsflyt
Rediger filer lokalt i `/Users/annarbohn/Code/nrk-rettelser/`, commit og push:
```bash
git add <filer>
git commit -m "beskrivelse"
git push
```
Sett opp Git-autentisering om nødvendig (`gh auth login` eller SSH-nøkkel).
