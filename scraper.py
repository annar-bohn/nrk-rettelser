import feedparser
import requests
from bs4 import BeautifulSoup
import json
import time
import os
from datetime import datetime

DATA_FILE = "data/corrections.json"
os.makedirs("data", exist_ok=True)

# Load existing
if os.path.exists(DATA_FILE):
    with open(DATA_FILE, encoding="utf-8") as f:
        corrections = json.load(f)
else:
    corrections = []

existing_urls = {c["url"] for c in corrections}

# Fetch RSS
feed = feedparser.parse("https://www.nrk.no/nyheter/siste.rss")

trigger_phrases = [
    "I en tidligere versjon",
    "NRK retter",
    "RETTELSE",
    "NRK har rettet",
    "Endringen er gjort",
    "rettet i artikkelen",
    "feilen ble rettet",
    "korrigert",
    "Rettelse:",
    "Vi har rettet"
]

print(f"Scanning {len(feed.entries)} recent articles...")

new_count = 0
for entry in feed.entries[:60]:
    url = entry.link
    if url in existing_urls:
        continue

    print(f"Checking {url}")
    try:
        r = requests.get(url, headers={"User-Agent": "NRK-Rettelser-Bot/1.0"}, timeout=12)
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text().lower()

        if any(phrase.lower() in text for phrase in trigger_phrases):
            # Extract correction block better
            correction_block = "Korrigert (se artikkelen for detaljer)"
            for tag in soup.find_all(['p', 'div', 'strong', 'h2']):
                t = tag.get_text(strip=True)
                if any(phrase in t for phrase in trigger_phrases):
                    correction_block = t[:600]
                    break

            new_entry = {
                "id": int(time.time()),
                "date": entry.published or datetime.now().isoformat(),
                "title": entry.title,
                "what": "Feil i tidligere versjon (automatisk oppdaget)",
                "correction": correction_block,
                "url": url,
                "auto": True
            }
            corrections.append(new_entry)
            existing_urls.add(url)
            new_count += 1
            print(f"✓ Added: {entry.title}")

        time.sleep(1.2)  # be gentle to NRK
    except Exception as e:
        print(f"Error on {url}: {e}")

# Save
with open(DATA_FILE, "w", encoding="utf-8") as f:
    json.dump(corrections, f, ensure_ascii=False, indent=2)

print(f"Done. Total corrections: {len(corrections)} | New this run: {new_count}")
