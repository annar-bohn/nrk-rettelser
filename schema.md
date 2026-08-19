# Data contract

Shared, cross-outlet record schema for `ctrlz.news`. `validate_data.py` is
the authoritative implementation — this document explains it in prose. If
the two ever disagree, the validator is right and this file is stale.

Written for Phase 1 of the SVT expansion (`.claude/plans/svt.md`). NRK is
live; SVT paths and fields below are the agreed target and do not exist yet.

## File layout

```
data/corrections_raw.json        NRK raw      (live)
data/corrections.json            NRK frontend (live, public URL)
data/svt/corrections_raw.json    SVT raw      (not yet created)
data/svt/corrections.json        SVT frontend (not yet created)
```

`validate_data.py` skips an outlet's checks with a printed note if its files
don't exist yet — that's expected for SVT until Phase 2, not a failure.

## Raw vs. frontend

Two files per outlet, same relationship for both:

- **Raw** (`corrections_raw.json`) — append-only working set. Every entry
  ever found, including ones QA rejected (`qa_status = not_a_correction`).
  Looser contract: only a handful of core fields are required, extra fields
  (full article text, scratch metadata) are expected and allowed.
- **Frontend** (`corrections.json`) — regenerated from raw on every
  enrichment run. Only entries whose `qa_status` is in `INCLUDE_STATUSES`
  (`genuine_correction`, `uncertain`, `pending`) survive; `not_a_correction`
  is filtered out. Strict contract: exact key set, no bulk text fields.

`INCLUDE_STATUSES = {genuine_correction, uncertain, pending}`.

Cross-file invariant: `set(frontend urls) == set(raw urls whose qa_status is
in INCLUDE_STATUSES)`. A mismatch means a bad regeneration and is always a
FAIL, never grandfathered.

## Field table

`required-when` — **raw**: one of the 9 core fields, always required.
**enrichment**: added by `enrich_qa.py`; absent or empty on entries still
`qa_status = pending`. **frontend**: part of the frontend file's exact key
set — always present in a frontend file.

| Field | Type | Required when | Outlet |
|---|---|---|---|
| `id` | int | raw, frontend | both |
| `url` | string | raw, frontend | both |
| `date` | string (ISO-8601) | raw, frontend | both |
| `title` | string | raw, frontend | both |
| `headline` | string | frontend (falls back to `title`) | both |
| `correction` | string | raw, frontend | both |
| `correction_text_raw` | string | raw, frontend | both |
| `correction_description` | string | frontend; enrichment | both |
| `correction_text_extract` | string | frontend; enrichment | both |
| `correction_date` | string `YYYY-MM-DD` or null | frontend; enrichment | both |
| `qa_status` | string enum | raw, frontend | both |
| `publication_date` | string (ISO-8601) or `""` | frontend; enrichment | both |
| `modified_date` | string (ISO-8601) or `""` | frontend; enrichment | both |
| `news_category` | string enum or `""` | frontend; enrichment | both |
| `correction_type` | string enum or `""` | frontend; enrichment | both |
| `journalist` | string | frontend; enrichment | both |
| `responsible_editor` | string | frontend; enrichment | both |
| `time_to_correct_hours` | number or null | frontend; enrichment | both |
| `auto` | bool | raw, frontend | both |
| `source` | string | raw, frontend | both |
| `nrk_section` | string | frontend | **NRK-legacy** |
| `section` | string | frontend | **SVT-from-day-one**; optional/additive on NRK |
| `outlet` | string, exactly `"nrk"` or `"svt"` | frontend | **SVT-from-day-one**; optional/additive on NRK |
| `what` | string | raw only | **NRK-legacy**, not adopted by SVT |
| `article_body` | string | raw only | both — **forbidden in frontend files** |
| `intro_text` | string | raw only | both — **forbidden in frontend files** |
| `qa_blocked` | bool | raw only, when Gemini safety-blocked the entry | both |

**NRK frontend exact key set** (21 keys — missing or extra is a FAIL):
`id, url, date, title, headline, correction, correction_text_raw,
correction_description, correction_text_extract, correction_date,
qa_status, nrk_section, publication_date, modified_date, news_category,
correction_type, journalist, responsible_editor, time_to_correct_hours,
auto, source`.

**SVT frontend exact key set** — identical set with `nrk_section` replaced
by `section` + `outlet` (`outlet` value must be exactly `"svt"`).

**Raw required core fields** (9, both outlets): `id, url, date, title,
correction, correction_text_raw, qa_status, auto, source`. Everything else
in a raw entry is optional and additional keys are allowed.

### Lifecycle nuance: pending entries

An entry with `qa_status = "pending"` has not been enriched yet (Gemini
never ran, or was permanently safety-blocked — see `qa_blocked` in
CLAUDE.md). For such an entry:

- **Frontend**: `news_category`, `correction_type`, `correction_description`
  may be `""`; `correction_date` may be `null`.
- **Raw**: the same fields may be `""` or **absent entirely** — enrichment
  never wrote them.

For any non-pending entry, `news_category` and `correction_type` must hold a
valid enum value if the field is present.

## Enums

`qa_status` (raw — all four): `genuine_correction`, `uncertain`,
`not_a_correction`, `pending`.
`qa_status` (frontend — `INCLUDE_STATUSES` only): `genuine_correction`,
`uncertain`, `pending`.

`news_category` (13): `sports`, `culture`, `politics`, `economy`, `science`,
`health`, `technology`, `local`, `world`, `crime`, `weather`,
`entertainment`, `other`.

`correction_type` (14): `factual_error`, `wrong_name`, `wrong_number`,
`wrong_image`, `wrong_date`, `wrong_location`, `mistranslation`,
`misleading_title`, `missing_context`, `source_error`, `retracted_claim`,
`spelling_grammar`, `attribution_error`, `other`.

Both enums are shared across outlets — same lists, same meaning. The
validator's `NEWS_CATEGORIES` / `CORRECTION_TYPES` constants are the single
source of truth; this table must match them.

## Quality invariants

- **Non-empty correction**: `correction` must not be empty after stripping.
- **No bare-label stubs**: `correction`, stripped and with a trailing `:`
  removed, uppercased, must not equal a bare label with no body text.
  NRK: `RETTING`, `RETTELSE`, `PRESISERING`. SVT (future): also `RÄTTELSE`,
  `FÖRTYDLIGANDE`.
- **No mojibake**: the byte sequences `Ã` and `â` (classic UTF-8-read-as-
  Latin-1 corruption) must never appear in `correction`, `title`, or
  `correction_description`.
- **URL shape**: `url` must start with `https://www.nrk.no/` (NRK) or
  `https://www.svt.se/` (SVT), and be unique within its file.

## Dates

Three date-shaped fields, three different rules:

- **`date`** (publication date, both raw and frontend): must parse as
  ISO-8601 after normalizing a trailing `Z` to `+00:00`
  (`datetime.fromisoformat`). **NRK is grandfathered** for a small,
  enumerated set of pre-existing bad values (see below) — any occurrence
  not on that list is a FAIL. **SVT is strict from day one**, no
  grandfather list; SVT emits this from JSON-LD and should never produce a
  malformed value.
- **`publication_date` / `modified_date`** (frontend only): ISO-8601 or
  empty string `""`. No grandfathered exceptions currently exist for these.
- **`correction_date`**: `null` or the literal string shape `YYYY-MM-DD`.
  Measured against the live NRK data: zero strays currently exist, so any
  occurrence is a FAIL, not a WARN — this field has never actually gone
  bad in practice.

### NRK's grandfathered `date` exceptions

Five URLs, pinned in `validate_data.py`'s `GRANDFATHERED["nrk"]["date_format"]`:

| URL | Value | Where |
|---|---|---|
| `.../krever-ett-ars-fengsel-for-isak-dreyer-1.17824825` | `Wed, 25 Mar 2026 13:29:20 GMT` | raw + frontend |
| `.../djesa-drommer-om-a-bli-gjeldfri-1.17668616` | `Wed, 21 Jan 2026 12:19:57 GMT` | raw + frontend |
| `.../psyk_-del-4_-lytt-til-de-andre-1.17171747` | `""` (empty) | raw + frontend |
| `.../karen-guiden_-en-guide-til-forbrukerrettigheter-1.17758500` | `Sun, 22 Mar 2026 06:43:51 GMT` | raw only (`not_a_correction`) |
| `.../_-stor-sjanse-for-at-dette-kan-vera-framtida-var-1.17687180` | `""` (empty) | raw only (`not_a_correction`) |

The first three are the "exactly 3 known warts" tracked in `CLAUDE.md`
(that count was always measured against the frontend file). The last two
only ever existed in raw, on entries QA rejected, so they never surfaced in
that count before — found while building this validator (2026-08-20), not
previously documented.

## `time_to_correct_hours`

`null`, or a number with `0 <= x <= 43800` (43800h ≈ 5 years, matching the
sanity bound already used in `enrich_qa.py`'s `calc_hours`).

## Grandfathering policy: pin current, fail new

`validate_data.py` keeps a `GRANDFATHERED` structure near the top of the
script: `outlet -> check name -> URL -> short reason`. It exists to let the
gate go green on day one despite a handful of known pre-existing data
issues, without hiding new ones:

- A violation on a `(check, url)` pair that **is** listed in
  `GRANDFATHERED` downgrades from FAIL to WARN.
- The same violation on any URL **not** listed is a FAIL.
- WARNs alone never fail the gate (exit 0); any FAIL does (exit 1).

Consequences:

- Grandfathering never grows silently — every WARN traces to an explicit,
  reasoned pin someone added on purpose.
- If NRK's scraper or enrichment ever produces the *same* class of bad data
  on a *new* URL, the gate goes red immediately — pinning only covers what
  already existed when the pin was written.
- SVT starts with an empty grandfather list. Nothing about SVT is
  pre-forgiven; its data either meets the contract or the gate fails.

As of 2026-08-20, two other violation classes exist in the live NRK data
beyond the date warts, both newly discovered while building this validator
and pinned so the gate can pass — flagged here for a maintainer decision,
not silently accepted as intended behavior:

- **`enum_news_category`**: two entries carry `news_category = "norge"`,
  which is not in the 13-value enum (`.../feil-i-nrk-sak-om-cruiseskipet-1.14491982`,
  `.../vedtak-om-martha-louise-og-durek-verretts-gin-omgjores-_-brot-ikke-loven-likevel-1.17320557`).
- **`raw_required_fields`**: one raw entry
  (`.../krever-ett-ars-fengsel-for-isak-dreyer-1.17824825` — the same entry
  as the first date wart above) is missing the required `source` field
  entirely.

## Running the validator

```bash
python3 validate_data.py --outlet all
```

`--outlet` accepts `nrk`, `svt`, or `all` (default `all`). Exit code 0 means
zero FAILs (WARNs may still be present and are printed). Read-only — it
never writes to any data file.
