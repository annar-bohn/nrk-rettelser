"""
Re-fetch article HTML for entries with bad or incomplete correction text.

Targets two categories:
  1. Entries where the correction text doesn't start with a trigger phrase
     (preamble wasn't trimmed due to missing sentence boundaries)
  2. Entries where the correction text appears cut off mid-sentence

Re-fetches the article, re-extracts using the same logic as the scraper
(with trim_to_correction), and updates the entry if the new extraction
is better.

Safe to run multiple times. Skips entries that are already clean.
Updates both corrections_raw.json and corrections.json.
"""

import json
import re
import time
import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "NRK-Rettelser-Bot/3.0 (+https://github.com/annar-bohn/nrk-rettelser)"}

TRIGGERS = [
    "i en tidligere versjon", "i en eldre versjon", "i en tidligere publisert versjon",
    "nrk retter", "nrk har rettet", "nrk korrigerer", "nrk beklager",
    "rettelse:", "rettelse", "retting:", "retting", "korrigering:",
    "presisering:", "endringen er gjort", "endringane er gjort",
    "endringane vart gjort", "det er gjort endringar", "vi har rettet",
    "artikkelen er oppdatert", "artikkelen er endra", "tidligere skrev vi",
    "etter publisering",
]

NAV_NOISE = ("hopp til innhold", "nrk tv", "nrk radio", "nrk super", "nrk p3")

BARE_TRIGGERS_RE = re.compile(r'\b(rettelse|retting)\b', re.IGNORECASE)


# ---------------------------------------------------------------------------
# Extraction functions (same as scraper.py)
# ---------------------------------------------------------------------------

def has_trigger(text):
    t = text.lower()
    for phrase in TRIGGERS:
        if phrase in ("rettelse", "retting"):
            continue
        if phrase in t:
            return True
    return bool(BARE_TRIGGERS_RE.search(t))


def is_nav_noise(text):
    t = text.lower()
    return any(t.startswith(prefix) for prefix in NAV_NOISE)


def trim_to_correction(text):
    t_lower = text.lower()

    # Check if text already starts with a trigger-like word
    if re.match(r'(?i)(rettels\w*|retting\w*|presisering\w*|korrigering\w*)', t_lower):
        return text

    earliest_pos = len(text)
    for phrase in TRIGGERS:
        if phrase in ("rettelse", "retting"):
            continue
        pos = t_lower.find(phrase)
        if pos != -1 and pos < earliest_pos:
            earliest_pos = pos

    if earliest_pos == len(text):
        m = BARE_TRIGGERS_RE.search(t_lower)
        if m:
            earliest_pos = m.start()

    if earliest_pos == len(text):
        return text

    chunk = text[:earliest_pos]

    boundary = chunk.rfind(". ")
    if boundary != -1:
        trimmed = text[boundary + 2:].strip()
        return trimmed if trimmed else text

    if chunk.endswith("."):
        trimmed = text[earliest_pos:].strip()
        return trimmed if trimmed else text

    m = re.search(r'\.[A-ZÆØÅ][^.]*$', chunk)
    if m:
        trimmed = text[m.start() + 1:].strip()
        return trimmed if trimmed else text

    last_nl = chunk.rfind("\n")
    if last_nl != -1:
        trimmed = text[last_nl + 1:].strip()
        return trimmed if trimmed else text

    return text.strip()


def extract_correction_blocks(soup):
    blocks = []

    for el in soup.find_all("p"):
        text = el.get_text(strip=True)
        if not text or len(text) > 2000 or is_nav_noise(text):
            continue
        if has_trigger(text):
            blocks.append(trim_to_correction(text)[:2000])

    for el in soup.find_all(["aside", "blockquote"]):
        text = el.get_text(strip=True)
        if not text or len(text) > 2000 or is_nav_noise(text):
            continue
        if has_trigger(text):
            blocks.append(trim_to_correction(text)[:2000])

    if not blocks:
        for el in soup.find_all("div"):
            if el.find(["p", "div"]):
                continue
            text = el.get_text(strip=True)
            if not text or len(text) > 2000 or is_nav_noise(text):
                continue
            if has_trigger(text):
                blocks.append(trim_to_correction(text)[:2000])

    if len(blocks) > 1:
        blocks.sort(key=len, reverse=True)
        deduped = []
        for b in blocks:
            if not any(b in existing for existing in deduped):
                deduped.append(b)
        blocks = deduped

    return " | ".join(blocks) if blocks else None


# ---------------------------------------------------------------------------
# Quality checks
# ---------------------------------------------------------------------------

def starts_with_trigger(text):
    """Check if text starts with a trigger phrase within first 50 chars."""
    t_lower = text[:80].lower()
    for phrase in TRIGGERS:
        if phrase in ("rettelse", "retting"):
            m = BARE_TRIGGERS_RE.search(t_lower)
            if m and m.start() < 50:
                return True
        else:
            pos = t_lower.find(phrase)
            if pos != -1 and pos < 50:
                return True
    return False


def looks_complete(text):
    """Check if text ends with proper punctuation."""
    t = text.rstrip()
    if not t:
        return False
    if t[-1] in '.!?)»"':
        return True
    if re.search(r'\d{2}\.\d{2}$', t):
        return True
    if re.search(r'\d{4}$', t):
        return True
    return False


def needs_refetch(text):
    """Return True if the entry's correction text needs re-fetching."""
    if not text:
        return True
    return not starts_with_trigger(text) or not looks_complete(text)


def is_better(new_text, old_text):
    """Return True if the new extraction is better than the old one."""
    if not new_text:
        return False
    new_starts = starts_with_trigger(new_text)
    old_starts = starts_with_trigger(old_text)
    new_complete = looks_complete(new_text)
    old_complete = looks_complete(old_text)

    # Better if it starts with trigger and old didn't
    if new_starts and not old_starts:
        return True
    # Better if it's complete and old wasn't
    if new_complete and not old_complete:
        return True
    # Better if both have trigger at start but new is complete and old isn't
    if new_starts and old_starts and new_complete and not old_complete:
        return True
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Load both data files
    with open("data/corrections_raw.json", encoding="utf-8") as f:
        raw_entries = json.load(f)
    with open("data/corrections.json", encoding="utf-8") as f:
        enriched_entries = json.load(f)

    # Index enriched entries by URL for fast lookup
    enriched_by_url = {e["url"]: e for e in enriched_entries}

    # Find entries that need re-fetching
    to_refetch = []
    for entry in raw_entries:
        text = entry.get("correction_text_raw", "") or entry.get("correction", "")
        if needs_refetch(text):
            to_refetch.append(entry)

    print(f"Found {len(to_refetch)} entries needing re-fetch out of {len(raw_entries)} total")

    updated_raw = 0
    updated_enriched = 0
    failed = 0
    skipped_no_improvement = 0

    for i, entry in enumerate(to_refetch):
        url = entry["url"]
        old_text = entry.get("correction_text_raw", "") or entry.get("correction", "")

        print(f"  [{i+1}/{len(to_refetch)}] {url[:80]}...", end=" ", flush=True)

        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                print(f"HTTP {r.status_code}")
                failed += 1
                time.sleep(0.5)
                continue

            soup = BeautifulSoup(r.text, "html.parser")
            new_text = extract_correction_blocks(soup)

            if new_text and is_better(new_text, old_text):
                entry["correction_text_raw"] = new_text
                entry["correction"] = new_text
                updated_raw += 1
                print("UPDATED")

                # Also update enriched entry if it exists
                if url in enriched_by_url:
                    enriched_by_url[url]["correction_text_raw"] = new_text
                    enriched_by_url[url]["correction"] = new_text
                    updated_enriched += 1
            else:
                skipped_no_improvement += 1
                reason = "no extraction" if not new_text else "not better"
                print(f"skipped ({reason})")

        except Exception as e:
            print(f"ERROR: {e}")
            failed += 1

        time.sleep(0.5)  # rate limit

    # Save
    with open("data/corrections_raw.json", "w", encoding="utf-8") as f:
        json.dump(raw_entries, f, ensure_ascii=False, indent=2)
    with open("data/corrections.json", "w", encoding="utf-8") as f:
        json.dump(enriched_entries, f, ensure_ascii=False, indent=2)

    print(f"\nDone.")
    print(f"  Updated (raw):      {updated_raw}")
    print(f"  Updated (enriched): {updated_enriched}")
    print(f"  No improvement:     {skipped_no_improvement}")
    print(f"  Failed:             {failed}")
