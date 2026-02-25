import feedparser
import requests
from bs4 import BeautifulSoup
import json
from datetime import datetime
import time
import os

DATA_FILE = "data/corrections.json"
os.makedirs("data", exist_ok=True)

# Load existing
if os.path.exists(DATA_FILE):
    with open(DATA_FILE) as f:
        corrections = json.load(f)
else:
    corrections = []

existing_urls = {c["url"] for c in corrections}

# Fetch latest NRK news RSS
feed = feedparser.parse("https://www.nrk.no/nyheter/siste.rss")

for entry in feed.entries[:30]:  # last 30 articles
    url = entry.link
    if url in existing_urls:
        continue

    print(f"Checking {url}")
    try:
        r = requests.get(url, headers={"User-Agent": "NRK-Rettelser-Bot/1.0 (+https://github.com/YOUR-USERNAME/nrk-rettelser)"}, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        
        # Look for correction blocks
        text = soup.get_text()
        if any(phrase in text for phrase in ["I en tidligere versjon", "NRK retter", "RETTELSE", "NRK har rettet", "Endringen er gjort"]):
            
            # Extract the whole correction paragraph(s)
            correction_block = ""
            for p in soup.find_all(["p", "div", "strong", "em"]):
                if any(phrase in p.get_text() for phrase in ["I en tidligere versjon", "NRK retter", "RETTELSE"]):
                    correction_block = p.get_text(strip=True)[:500] + "..." if len(p.get_text()) > 500 else p.get_text(strip=True)
                    break
            if not correction_block:
                correction_block = "Korrigert (detaljer i artikkelen)"

            new_entry = {
                "id": int(time.time()),
                "date": entry.published,
                "title": entry.title,
                "what": "Feil i tidligere versjon (automatisk oppdaget)",
                "correction": correction_block,
                "url": url,
                "auto": True
            }
            corrections.append(new_entry)
            existing_urls.add(url)
            print(f"→ Added correction: {entry.title}")
        
        time.sleep(1.5)  # be nice to NRK servers
    except Exception as e:
        print(f"Error on {url}: {e}")

# Save
with open(DATA_FILE, "w", encoding="utf-8") as f:
    json.dump(corrections, f, ensure_ascii=False, indent=2)

print(f"Done. Total corrections: {len(corrections)}")
