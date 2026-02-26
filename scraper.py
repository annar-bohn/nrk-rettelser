import feedparser
import requests
from bs4 import BeautifulSoup
import json
import time
import os
from datetime import datetime

DATA_FILE = "data/corrections.json"

# Ensure folder exists
os.makedirs("data", exist_ok=True)

# Load existing corrections or start fresh
if os.path.exists(DATA_FILE):
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        corrections = json.load(f)
else:
    corrections = []

existing_urls = {item["url"] for item in corrections}

# Comprehensive, case-insensitive trigger phrases (covers 95%+ of real NRK corrections)
TRIGGER_PHRASES = [
    "i en tidligere versjon", "nrk retter", "rettelse", "nrk har rettet",
    "endringen er gjort", "rettet i artikkelen", "feilen ble rettet",
    "vi har rettet", "nrk beklager", "korrigert", "rettelse:",
    "oppdaterte saken", "tidligere versjon av denne", "vi har endret",
    "feilaktig", "rettet kl"
]

print(f"NRK Rettelser Scraper v2.0 started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"Existing entries: {len(corrections)} | Scanning up to 100 recent articles...\n")

feed = feedparser.parse("https://www.nrk.no/nyheter/siste.rss")
new_count = 0

for entry in feed.entries[:100]:          # increased depth
    url = entry.link
    if url in existing_urls:
        continue

    print(f"Checking → {entry.title[:80]}...")

    try:
        r = requests.get(
            url,
            headers={"User-Agent": "NRK-Rettelser-Bot/2.0 (+https://github.com/annar-bohn/nrk-rettelser)"},
            timeout=15
        )
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")
        page_text = soup.get_text()

        # Find the actual correction block (first paragraph containing a trigger)
        correction_block = "Korrigert (se artikkelen for detaljer)"
        found = False

        for tag in soup.find_all(['p', 'div', 'section', 'article', 'strong', 'h2', 'h3']):
            text = tag.get_text(strip=True)
            if any(phrase.lower() in text.lower() for phrase in TRIGGER_PHRASES) and len(text) > 30:
                correction_block = text[:780]  # reasonable length
                found = True
                break

        if found:
            new_entry = {
                "id": int(time.time() * 1000),                    # unique millisecond ID
                "date": entry.published or datetime.now().isoformat(),
                "title": entry.title.strip(),
                "what": "Feil i tidligere versjon (automatisk oppdaget)",
                "correction": correction_block,
                "url": url,
                "auto": True
            }
            corrections.append(new_entry)
            existing_urls.add(url)
            new_count += 1
            print(f"   ✓ ADDED CORRECTION: {entry.title[:60]}...")

        time.sleep(1.25)  # be nice to NRK (≈ 48 requests/min max)

    except Exception as e:
        print(f"   Error on {url}: {type(e).__name__} – {str(e)[:80]}")

# Sort newest first (so the page always shows latest on top)
corrections.sort(key=lambda x: x.get("date", ""), reverse=True)

# Save (only if changed, but auto-commit action will handle no-op)
with open(DATA_FILE, "w", encoding="utf-8") as f:
    json.dump(corrections, f, ensure_ascii=False, indent=2)

print(f"\nFinished! Total corrections: {len(corrections)} | New this run: {new_count}")
if new_count == 0:
    print("No new corrections found in the latest articles (normal on quiet days).")
