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
   - genuine_correction — artikkelen inneholder en tydelig rettelse, presisering eller beklagelse
   - uncertain — det er uklart om dette er en reell rettelse
   - not_a_correction — dette er IKKE en rettelse (f.eks. vanlig oppdatering, falsk positiv fra scraper)

2. Trekk ut en kort beskrivelse av hva som ble rettet (maks 200 tegn), på norsk. Hvis usikkert eller ikke en rettelse, sett tom streng.

3. Oppgi datoen for rettelsen i ISO 8601-format (YYYY-MM-DD) hvis den kan utledes. Ellers null.

4. Kategoriser nyhetstype (news_category). Velg én:
   sports, culture, politics, economy, science, health, technology, local, world, crime, weather, entertainment, other

5. Klassifiser feiltype (correction_type). Velg én:
   factual_error, wrong_name, wrong_number, wrong_image, wrong_date, wrong_location, mistranslation, misleading_title, missing_context, source_error, retracted_claim, spelling_grammar, attribution_error, other

6. Journalist (bekreft eller korriger): bruk informasjon fra artikkelen. Tom streng hvis ukjent.

7. Ansvarlig redaktør: bruk informasjon fra artikkelen. Tom streng hvis ukjent.
{custom_fields_instructions}

Svar KUN med gyldig JSON i dette formatet (ingen markdown, ingen forklaringer utenfor JSON):
{{
  "qa_status": "genuine_correction" | "uncertain" | "not_a_correction",
  "correction_description": "...",
  "correction_date": "YYYY-MM-DD" | null,
  "news_category": "...",
  "correction_type": "...",
  "journalist": "...",
  "responsible_editor": "..."
  {custom_fields_json}
}}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_metadata(html, url):
    """Extract structured metadata from NRK article HTML."""
    soup = BeautifulSoup(html, "html.parser")
    meta = {}

    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        meta["headline"] = og_title["content"].strip()[:300]
    else:
        h1 = soup.find("h1")
        if h1:
            meta["headline"] = h1.get_text(strip=True)[:300]
        else:
            meta["headline"] = ""

    try:
        path = url.replace("https://www.nrk.no/", "").replace("http://www.nrk.no/", "")
        section = path.split("/")[0] if "/" in path else path.split("?")[0]
        meta["nrk_section"] = section if section else "ukjent"
    except Exception:
        meta["nrk_section"] = "ukjent"

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
    match = re.search(
        r"Ansvarlig\s+redakt[øo]r[:\s]+([A-ZÆØÅ][a-zæøå]+(?: [A-ZÆØÅ][a-zæøå]+){1,4})",
        full_text,
    )
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


def call_gemini(prompt):
    """
    Call the Gemini API with the given prompt.
    Returns a parsed dict, "RATE_LIMITED" on 429, or None on error.
    """
    if not GEMINI_API_KEY:
        print("  [Gemini] No API key set, skipping.")
        return None

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1},
    }
    try:
        resp = requests.post(GEMINI_URL, json=payload, timeout=30)
        if resp.status_code == 429:
            print("  [Gemini] Rate limited (429).")
            return "RATE_LIMITED"
        if resp.status_code != 200:
            print(f"  [Gemini] Error {resp.status_code}: {resp.text[:200]}")
            return None

        data = resp.json()
        text = (
            data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )

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
        return None
    except Exception as e:
        print(f"  [Gemini] Exception: {e}")
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


def process_entry(entry):
    url = entry.get("url", "")
    print(f"  Processing: {url}")

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        html = resp.text
    except Exception as e:
        print(f"  [Fetch] Error: {e}")
        entry["qa_status"] = "uncertain"
        return True

    meta = extract_metadata(html, url)

    if not entry.get("headline") and entry.get("title"):
        entry["headline"] = entry["title"]
    if not entry.get("correction_text_raw") and entry.get("correction"):
        entry["correction_text_raw"] = entry["correction"]

    for field in ["headline", "nrk_section", "publication_date", "modified_date",
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

    prompt = QA_PROMPT_TEMPLATE.format(
        url=url,
        headline=entry.get("headline", ""),
        nrk_section=entry.get("nrk_section", ""),
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

    result = call_gemini(prompt)

    if result == "RATE_LIMITED":
        return False

    if result is None or not isinstance(result, dict):
        entry["qa_status"] = "uncertain"
        return True

    entry["qa_status"] = result.get("qa_status", "uncertain")
    entry["correction_description"] = result.get("correction_description", "")
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

    entry["time_to_correct_hours"] = calc_hours(
        entry.get("publication_date", ""),
        entry.get("correction_date"),
    )

    if entry.get("article_body"):
        entry["article_body"] = entry["article_body"][:20000]

    print(f"  -> qa_status={entry['qa_status']}, type={entry.get('correction_type')}")
    return True


def run(raw_path, output_path, max_entries=450):
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

    pending = [e for e in entries if e.get("qa_status") == "pending"]
    # Sort newest first so top-of-page articles get enriched first
    pending.sort(key=lambda x: x.get("date") or "", reverse=True)
    pending = pending[:max_entries]
    print(f"Found {len(pending)} pending entries to process (max {max_entries})")

    rate_limited = False
    processed = 0

    for entry in pending:
        if rate_limited:
            break

        ok = process_entry(entry)
        processed += 1

        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)

        if not ok:
            print("Rate limited — saving progress and stopping.")
            rate_limited = True
            break

        time.sleep(1)

    print(f"Processed {processed} entries.")

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
            "correction_date": entry.get("correction_date"),
            "qa_status": status,
            "nrk_section": entry.get("nrk_section", ""),
            "publication_date": entry.get("publication_date", ""),
            "modified_date": entry.get("modified_date", ""),
            "news_category": entry.get("news_category", ""),
            "correction_type": entry.get("correction_type", ""),
            "journalist": entry.get("journalist", ""),
            "responsible_editor": entry.get("responsible_editor", ""),
            "time_to_correct_hours": entry.get("time_to_correct_hours"),
            "auto": entry.get("auto", True),
            "source": entry.get("source", ""),
        }
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
    parser.add_argument("--max-entries", type=int, default=450, help="Max entries to enrich per run (default 450)")
    args = parser.parse_args()
    run("data/corrections_raw.json", "data/corrections.json", max_entries=args.max_entries)
