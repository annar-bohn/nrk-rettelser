import os
import json
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-3.1-flash-lite-preview"  # 500 RPD free tier
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    + GEMINI_MODEL + ":generateContent?key=" + GEMINI_API_KEY
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "no,nb;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Custom fields — both disabled by default
CUSTOM_FIELDS = {
    "enabled": False,
    "fields": [
        # {"name": "example_field", "description": "Example description", "enabled": False},
    ],
}

# The machine-readable half of both prompts: the JSON response contract
# Gemini must follow. Shared as one constant (rather than duplicated per
# language) because this is the part that actually has to match across
# outlets byte-for-byte — field names and enum values, not prose. Drift here
# is exactly the failure mode that produced the news_category="norge" wart
# (a value outside the enum both sides are supposed to share).
QA_RESPONSE_JSON_CONTRACT = """{{
  "qa_status": "genuine_correction" | "uncertain" | "not_a_correction",
  "correction_description": "...",
  "correction_text_extract": "...",
  "correction_date": "YYYY-MM-DD" | null,
  "news_category": "...",
  "correction_type": "...",
  "journalist": "...",
  "responsible_editor": "..."
  {custom_fields_json}
}}
"""

QA_PROMPT_TEMPLATE = """Du er en redaksjonell kvalitetskontrollør for NRK-rettelser.

Her er informasjon om en artikkel med en mulig rettelse:

URL: {url}
Overskrift: {headline}
Seksjon: {nrk_section}
Publiseringsdato: {publication_date}
Ingresstekst: {intro_text}
Journalist: {journalist}
Ansvarlig redaktør: {responsible_editor}
Artikkeltekst (utdrag): {article_body}
Rettelsestekst funnet av scraper: {correction_text_raw}
Rettelsesdato (fra scraper): {correction_date_raw}

Oppgave:
1. Klassifiser om dette er en ekte rettelse. Svar med ett av:
   - genuine_correction — artikkelen inneholder en tydelig rettelse, presisering eller beklagelse der NRK innrømmer en feil
   - uncertain — det er uklart om dette er en reell rettelse
   - not_a_correction — dette er IKKE en rettelse. Eksempler: vanlig oppdatering med ny informasjon (f.eks. «etter publisering har X skjedd»), løpende nyhetssaker som oppdateres med nye fakta, artikler som er oppdatert uten at NRK har gjort noe feil, falsk positiv fra scraper

2. Trekk ut en kort beskrivelse av hva som ble rettet (maks 200 tegn), på norsk. Hvis usikkert eller ikke en rettelse, sett tom streng.

3. Oppgi datoen for rettelsen i ISO 8601-format (YYYY-MM-DD) hvis den kan utledes. Ellers null.

4. Kategoriser nyhetstype (news_category). Velg én:
   sports, culture, politics, economy, science, health, technology, local, world, crime, weather, entertainment, other

5. Klassifiser feiltype (correction_type). Velg én:
   factual_error, wrong_name, wrong_number, wrong_image, wrong_date, wrong_location, mistranslation, misleading_title, missing_context, source_error, retracted_claim, spelling_grammar, attribution_error, other

6. Journalist (bekreft eller korriger): bruk informasjon fra artikkelen. Tom streng hvis ukjent.

7. Ansvarlig redaktør: bruk informasjon fra artikkelen. Tom streng hvis ukjent.

8. Trekk ut den eksakte rettelsesteksten (correction_text_extract) — kopier ordrett den delen av artikkelen som omhandler rettelsen/presiseringen/beklagelsen. Start fra der rettelsesnotisen begynner (f.eks. «NRK retter:», «Rettelse:», «Presisering:» osv.) og inkluder hele rettelsesavsnittet. Ikke ta med artikkelens vanlige innhold, AI-oppsummeringer eller navigasjon. Maks 2000 tegn. Tom streng hvis ikke en rettelse.
{custom_fields_instructions}

Svar KUN med gyldig JSON i dette formatet (ingen markdown, ingen forklaringer utenfor JSON):
""" + QA_RESPONSE_JSON_CONTRACT

QA_PROMPT_TEMPLATE_SV = """Du är en redaktionell kvalitetskontrollant för SVT-rättelser.

Här är information om en artikel med en möjlig rättelse:

URL: {url}
Rubrik: {headline}
Sektion: {section}
Publiceringsdatum: {publication_date}
Ingress: {intro_text}
Journalist: {journalist}
Ansvarig utgivare: {responsible_editor}
Artikeltext (utdrag): {article_body}
Rättelsetext funnen av scrapern: {correction_text_raw}
Rättelsedatum (från scrapern): {correction_date_raw}

Uppgift:
1. Klassificera om detta är en riktig rättelse. Svara med ett av:
   - genuine_correction — artikeln innehåller en tydlig rättelse, ett förtydligande eller en ursäkt där SVT medger ett fel
   - uncertain — det är oklart om detta är en verklig rättelse
   - not_a_correction — detta är INTE en rättelse. Exempel: en vanlig uppdatering med ny information (t.ex. "Artikeln är uppdaterad" i takt med att händelseförloppet utvecklas), en löpande nyhetshändelse eller livebloggpost som uppdateras med nya fakta i takt med att de blir kända, falsk positiv från scrapern

2. Ta fram en kort beskrivning av vad som rättades (max 200 tecken), på svenska. Om osäkert eller inte en rättelse, sätt tom sträng.

3. Ange datumet för rättelsen i ISO 8601-format (YYYY-MM-DD) om det kan härledas. Annars null.

4. Kategorisera nyhetstyp (news_category). Välj en:
   sports, culture, politics, economy, science, health, technology, local, world, crime, weather, entertainment, other

5. Klassificera feltyp (correction_type). Välj en:
   factual_error, wrong_name, wrong_number, wrong_image, wrong_date, wrong_location, mistranslation, misleading_title, missing_context, source_error, retracted_claim, spelling_grammar, attribution_error, other

6. Journalist (bekräfta eller korrigera): använd information från artikeln. Tom sträng om okänd.

7. Ansvarig utgivare: använd information från artikeln. Tom sträng om okänd.

8. Ta fram den exakta rättelsetexten (correction_text_extract) — kopiera ordagrant den del av artikeln som handlar om rättelsen/förtydligandet/ursäkten. Börja där rättelsenotisen börjar (t.ex. "Rättelse:", "Förtydligande:", "SVT rättar", "I en tidigare version …") och inkludera hela rättelsestycket. Ta inte med artikelns vanliga innehåll, AI-sammanfattningar eller navigering. Max 2000 tecken. Tom sträng om det inte är en rättelse.
{custom_fields_instructions}

Svara ENDAST med giltig JSON i detta format (ingen markdown, inga förklaringar utanför JSON):
""" + QA_RESPONSE_JSON_CONTRACT

# Per-outlet config: raw/frontend paths, QA prompt template, the name of the
# section field on raw entries (NRK's legacy `nrk_section` vs. SVT's generic
# `section`), whether that section is derived from the URL path (NRK) or
# supplied by the collector (SVT), and the responsible-editor regex. Adding
# an outlet means adding one entry here — no `if outlet == "..."` branches
# elsewhere should be needed.
OUTLET_CONFIG = {
    "nrk": {
        "raw_path": "data/corrections_raw.json",
        "frontend_path": "data/corrections.json",
        "prompt_template": QA_PROMPT_TEMPLATE,
        "section_field": "nrk_section",
        "derive_section_from_url": True,
        "responsible_editor_pattern": (
            r"Ansvarlig\s+redakt[øo]r[:\s]+"
            r"([A-ZÆØÅ][a-zæøå]+(?: [A-ZÆØÅ][a-zæøå]+){1,4})"
        ),
    },
    "svt": {
        "raw_path": "data/svt/corrections_raw.json",
        "frontend_path": "data/svt/corrections.json",
        "prompt_template": QA_PROMPT_TEMPLATE_SV,
        "section_field": "section",
        "derive_section_from_url": False,
        # SVT's byline runs the editor name straight into the "Uppdaterad"/
        # "Publicerad" timestamp label with only a space, e.g. "Ansvarig
        # utgivare: Karin Ekman Uppdaterad 29 augusti 2026 kl 07:16" — a
        # bare {1,4}-word repeat (as NRK's pattern uses) swallows that label
        # into the name. Exclude those two tokens per extra word so the
        # match stops at the name.
        "responsible_editor_pattern": (
            r"Ansvarig\s+utgivare[:\s]+([A-ZÅÄÖ][a-zåäö]+"
            r"(?: (?!Uppdaterad\b|Publicerad\b)[A-ZÅÄÖ][a-zåäö]+){0,3})"
        ),
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_metadata(html, url, outlet="nrk"):
    """Extract structured metadata from an outlet's article HTML."""
    soup = BeautifulSoup(html, "html.parser")
    meta = {}
    config = OUTLET_CONFIG[outlet]
    section_field = config["section_field"]

    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        meta["headline"] = og_title["content"].strip()[:300]
    else:
        h1 = soup.find("h1")
        if h1:
            meta["headline"] = h1.get_text(strip=True)[:300]
        else:
            meta["headline"] = ""

    if config["derive_section_from_url"]:
        try:
            path = url.replace("https://www.nrk.no/", "").replace("http://www.nrk.no/", "")
            section = path.split("/")[0] if "/" in path else path.split("?")[0]
            meta[section_field] = section if section else "ukjent"
        except Exception:
            meta[section_field] = "ukjent"
    # Outlets with derive_section_from_url=False (SVT today) have their
    # collector set `section` already — don't derive or overwrite it here.

    pub_meta = soup.find("meta", property="article:published_time")
    if pub_meta and pub_meta.get("content"):
        meta["publication_date"] = pub_meta["content"].strip()
    else:
        meta["publication_date"] = ""

    mod_meta = soup.find("meta", property="article:modified_time")
    if mod_meta and mod_meta.get("content"):
        meta["modified_date"] = mod_meta["content"].strip()
    else:
        meta["modified_date"] = ""

    og_desc = soup.find("meta", property="og:description")
    if og_desc and og_desc.get("content"):
        meta["intro_text"] = og_desc["content"].strip()[:500]
    else:
        meta["intro_text"] = ""

    journalist = ""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict):
                author = data.get("author")
                if isinstance(author, dict):
                    journalist = author.get("name", "").strip()
                elif isinstance(author, list) and author:
                    journalist = author[0].get("name", "").strip()
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("author"):
                        author = item["author"]
                        if isinstance(author, dict):
                            journalist = author.get("name", "").strip()
                        elif isinstance(author, list) and author:
                            journalist = author[0].get("name", "").strip()
                        if journalist:
                            break
        except Exception:
            pass
        if journalist:
            break

    if not journalist:
        byline = soup.find(class_=lambda c: c and "byline" in c.lower())
        if byline:
            journalist = byline.get_text(strip=True)[:100]

    meta["journalist"] = journalist

    responsible_editor = ""
    full_text = soup.get_text(separator=" ")
    import re
    match = re.search(config["responsible_editor_pattern"], full_text)
    if match:
        responsible_editor = match.group(1).strip()
    meta["responsible_editor"] = responsible_editor

    article_el = soup.find("article")
    if article_el:
        body_text = article_el.get_text(separator=" ", strip=True)
    else:
        body_text = soup.get_text(separator=" ", strip=True)
    meta["article_body"] = body_text[:20000]

    return meta


def extract_text(data):
    """
    Pull the answer text out of a generateContent response.

    Thinking models return several parts per candidate, and the reasoning ones
    (marked "thought") carry no usable text — reading parts[0] blindly picks up
    a thought part and yields "". Walk every part and keep the real text.
    """
    out = []
    for cand in data.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            if part.get("thought"):
                continue
            if part.get("text"):
                out.append(part["text"])
    return "".join(out)


def call_gemini(prompt):
    """
    Call the Gemini API with the given prompt.
    Returns a parsed dict, "RATE_LIMITED" on 429 (after retries), or None on error.
    """
    if not GEMINI_API_KEY:
        print("  [Gemini] No API key set, skipping.")
        return None

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1},
    }
    max_retries = 3
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(GEMINI_URL, json=payload, timeout=30)
            if resp.status_code == 429:
                if attempt < max_retries:
                    wait = 60 * (attempt + 1)
                    print(f"  [Gemini] Rate limited (429), waiting {wait}s (attempt {attempt + 1}/{max_retries})...")
                    time.sleep(wait)
                    continue
                print("  [Gemini] Rate limited (429) after all retries.")
                return "RATE_LIMITED"
            if resp.status_code != 200:
                print(f"  [Gemini] Error {resp.status_code}: {resp.text[:200]}")
                return None

            data = resp.json()
            text = extract_text(data)

            if not text:
                # A 200 with no usable text means the model answered but we got
                # nothing to parse — log enough to tell why instead of failing
                # with a bare JSONDecodeError further down.
                cand = (data.get("candidates") or [{}])[0]
                block_reason = (data.get("promptFeedback") or {}).get("blockReason")
                finish_reason = cand.get("finishReason")
                print(
                    f"  [Gemini] Empty response. finishReason={finish_reason} "
                    f"promptFeedback={data.get('promptFeedback')} "
                    f"usage={data.get('usageMetadata')}"
                )
                print(f"  [Gemini] Raw: {json.dumps(data)[:600]}")
                # A safety block is permanent for this article — retrying it
                # every run forever burns quota and never succeeds.
                if block_reason or finish_reason in ("SAFETY", "PROHIBITED_CONTENT"):
                    return "BLOCKED"
                return None

            text = text.strip()
            if text.startswith("```"):
                lines = text.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                text = "\n".join(lines).strip()

            return json.loads(text)
        except json.JSONDecodeError as e:
            print(f"  [Gemini] JSON parse error: {e}")
            print(f"  [Gemini] Unparsable text: {text[:400]!r}")
            return None
        except Exception as e:
            print(f"  [Gemini] Exception: {e}")
            return None
    return None


def calc_hours(pub_date_str, corr_date_str):
    if not pub_date_str or not corr_date_str:
        return None
    try:
        pub = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
        if len(corr_date_str) == 10:
            corr = datetime.fromisoformat(corr_date_str + "T12:00:00+00:00")
        else:
            corr = datetime.fromisoformat(corr_date_str.replace("Z", "+00:00"))
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        if corr.tzinfo is None:
            corr = corr.replace(tzinfo=timezone.utc)
        delta = corr - pub
        hours = delta.total_seconds() / 3600
        if hours < 0 or hours > 24 * 365 * 5:
            return None
        return round(hours, 2)
    except Exception:
        return None


def process_entry(entry, outlet="nrk"):
    url = entry.get("url", "")
    print(f"  Processing: {url}")

    config = OUTLET_CONFIG[outlet]
    section_field = config["section_field"]

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        # Bytes, not resp.text — /artikkel/ pages omit the charset and would
        # otherwise be decoded as ISO-8859-1.
        html = resp.content
    except Exception as e:
        print(f"  [Fetch] Error: {e} — will retry next run")
        return True  # leave as pending for retry

    meta = extract_metadata(html, url, outlet=outlet)

    if not entry.get("headline") and entry.get("title"):
        entry["headline"] = entry["title"]
    if not entry.get("correction_text_raw") and entry.get("correction"):
        entry["correction_text_raw"] = entry["correction"]

    for field in ["headline", section_field, "publication_date", "modified_date",
                  "intro_text", "journalist", "responsible_editor", "article_body"]:
        if not entry.get(field):
            entry[field] = meta.get(field, "")

    correction_text_raw = entry.get("correction_text_raw") or entry.get("correction", "")

    custom_fields_instructions = ""
    custom_fields_json = ""
    if CUSTOM_FIELDS.get("enabled"):
        active = [f for f in CUSTOM_FIELDS.get("fields", []) if f.get("enabled")]
        if active:
            instructions = []
            json_fields = []
            for i, cf in enumerate(active, 8):
                instructions.append(f'{i}. {cf["name"]}: {cf["description"]}')
                json_fields.append(f'  "{cf["name"]}": "..."')
            custom_fields_instructions = "\n".join(instructions)
            custom_fields_json = ",\n" + ",\n".join(json_fields)

    fmt_kwargs = dict(
        url=url,
        headline=entry.get("headline", ""),
        publication_date=entry.get("publication_date", ""),
        intro_text=entry.get("intro_text", ""),
        journalist=entry.get("journalist", ""),
        responsible_editor=entry.get("responsible_editor", ""),
        article_body=entry.get("article_body", "")[:20000],
        correction_text_raw=correction_text_raw[:2000],
        correction_date_raw=entry.get("date", ""),
        custom_fields_instructions=custom_fields_instructions,
        custom_fields_json=custom_fields_json,
    )
    fmt_kwargs[section_field] = entry.get(section_field, "")

    prompt = config["prompt_template"].format(**fmt_kwargs)

    result = call_gemini(prompt)

    if result == "RATE_LIMITED":
        return False

    if result == "BLOCKED":
        # Gemini's safety filter refuses this article (typically crime reporting
        # on abuse cases). It will never enrich, so stop retrying it. The entry
        # still reaches the frontend with its raw correction text.
        entry["qa_blocked"] = True
        print("  [Gemini] Blocked by safety filter — flagged, will not retry")
        return True

    if result is None or not isinstance(result, dict):
        print(f"  [Gemini] No valid response — will retry next run")
        return True  # leave as pending for retry

    entry["qa_status"] = result.get("qa_status", "uncertain")
    entry["correction_description"] = result.get("correction_description", "")
    entry["correction_text_extract"] = result.get("correction_text_extract", "")
    entry["correction_date"] = result.get("correction_date")
    entry["news_category"] = result.get("news_category", "other")
    entry["correction_type"] = result.get("correction_type", "other")

    if result.get("journalist"):
        entry["journalist"] = result["journalist"]
    if result.get("responsible_editor"):
        entry["responsible_editor"] = result["responsible_editor"]

    if CUSTOM_FIELDS.get("enabled"):
        for cf in CUSTOM_FIELDS.get("fields", []):
            if cf.get("enabled") and cf["name"] in result:
                entry[cf["name"]] = result[cf["name"]]

    # Prefer modified_date (reliable outlet metadata) over correction_date (Gemini guess)
    corr_date = entry.get("modified_date") or entry.get("correction_date")
    entry["time_to_correct_hours"] = calc_hours(
        entry.get("publication_date", ""),
        corr_date,
    )

    if entry.get("article_body"):
        entry["article_body"] = entry["article_body"][:20000]

    print(f"  -> qa_status={entry['qa_status']}, type={entry.get('correction_type')}")
    return True


def run(raw_path, output_path, outlet="nrk", max_entries=450):
    section_field = OUTLET_CONFIG[outlet]["section_field"]
    os.makedirs(os.path.dirname(raw_path), exist_ok=True)

    if os.path.exists(raw_path):
        with open(raw_path, encoding="utf-8") as f:
            entries = json.load(f)
    else:
        entries = []

    print(f"Loaded {len(entries)} entries from {raw_path}")

    for entry in entries:
        if not entry.get("correction_text_raw") and entry.get("correction"):
            entry["correction_text_raw"] = entry["correction"]
        if not entry.get("headline") and entry.get("title"):
            entry["headline"] = entry["title"]

    pending = [
        e for e in entries
        if e.get("qa_status") == "pending" and not e.get("qa_blocked")
    ]
    # Sort newest first so top-of-page articles get enriched first
    pending.sort(key=lambda x: x.get("date") or "", reverse=True)
    pending = pending[:max_entries]
    blocked = sum(1 for e in entries if e.get("qa_blocked"))
    print(f"Found {len(pending)} pending entries to process (max {max_entries})")
    if blocked:
        print(f"Skipping {blocked} entries permanently blocked by the safety filter")

    rate_limited = False
    processed = 0

    for entry in pending:
        if rate_limited:
            break

        ok = process_entry(entry, outlet=outlet)
        processed += 1

        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)

        if not ok:
            print("Rate limited — saving progress and stopping.")
            rate_limited = True
            break

        time.sleep(4)  # Stay under 15 RPM Gemini rate limit

    print(f"Processed {processed} entries.")

    # Recalculate time_to_correct_hours for ALL entries using modified_date preference
    recalc_count = 0
    for entry in entries:
        if not entry.get("publication_date"):
            continue
        corr_date = entry.get("modified_date") or entry.get("correction_date")
        new_ttc = calc_hours(entry.get("publication_date", ""), corr_date)
        if new_ttc != entry.get("time_to_correct_hours"):
            entry["time_to_correct_hours"] = new_ttc
            recalc_count += 1
    if recalc_count:
        print(f"Recalculated time_to_correct_hours for {recalc_count} entries.")

    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)

    INCLUDE_STATUSES = {"genuine_correction", "uncertain", "pending"}

    frontend_entries = []
    for entry in entries:
        status = entry.get("qa_status", "pending")
        if status not in INCLUDE_STATUSES:
            continue

        fe = {
            "id": entry.get("id"),
            "url": entry.get("url", ""),
            "date": entry.get("date", ""),
            "title": entry.get("headline") or entry.get("title", ""),
            "headline": entry.get("headline") or entry.get("title", ""),
            "correction": entry.get("correction_text_raw") or entry.get("correction", ""),
            "correction_text_raw": entry.get("correction_text_raw") or entry.get("correction", ""),
            "correction_description": entry.get("correction_description", ""),
            "correction_text_extract": entry.get("correction_text_extract", ""),
            "correction_date": entry.get("correction_date"),
            "qa_status": status,
        }
        # NRK keeps its legacy `nrk_section` key in its original position;
        # SVT emits the generic `section` in its place (plus `outlet` below).
        # This preserves the NRK frontend file's exact key order so a
        # default-args regeneration is byte-identical to what's committed.
        fe[section_field] = entry.get(section_field, "")
        fe.update({
            "publication_date": entry.get("publication_date", ""),
            "modified_date": entry.get("modified_date", ""),
            "news_category": entry.get("news_category", ""),
            "correction_type": entry.get("correction_type", ""),
            "journalist": entry.get("journalist", ""),
            "responsible_editor": entry.get("responsible_editor", ""),
            "time_to_correct_hours": entry.get("time_to_correct_hours"),
            "auto": entry.get("auto", True),
            "source": entry.get("source", ""),
        })
        if outlet != "nrk":
            fe["outlet"] = outlet
        frontend_entries.append(fe)

    frontend_entries.sort(key=lambda x: x.get("date") or "", reverse=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(frontend_entries, f, ensure_ascii=False, indent=2)

    genuine = sum(1 for e in frontend_entries if e["qa_status"] == "genuine_correction")
    uncertain = sum(1 for e in frontend_entries if e["qa_status"] == "uncertain")
    still_pending = sum(1 for e in frontend_entries if e["qa_status"] == "pending")

    print(
        f"Frontend file written: {len(frontend_entries)} entries "
        f"({genuine} genuine, {uncertain} uncertain, {still_pending} pending)"
    )
    print(f"Raw file: {len(entries)} entries total.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--outlet", choices=["nrk", "svt"], default="nrk", help="Outlet to enrich (default nrk)")
    parser.add_argument("--max-entries", type=int, default=450, help="Max entries to enrich per run (default 450)")
    args = parser.parse_args()
    outlet_config = OUTLET_CONFIG[args.outlet]
    run(outlet_config["raw_path"], outlet_config["frontend_path"], outlet=args.outlet, max_entries=args.max_entries)
