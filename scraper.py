import feedparser
import requests
from bs4 import BeautifulSoup
import json
import time
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

DATA_FILE = "data/corrections.json"
os.makedirs("data", exist_ok=True)

HEADERS = {"User-Agent": "NRK-Rettelser-Bot/2.0 (+https://github.com/annar-bohn/nrk-rettelser)"}

RSS_FEEDS = [
    "https://www.nrk.no/toppsaker.rss",
    "https://www.nrk.no/nyheter/siste.rss",
    "https://www.nrk.no/sport/siste.rss",
    "https://www.nrk.no/kultur/siste.rss",
    "https://www.nrk.no/livsstil/siste.rss",
]

TRIGGERS = [
    "i en tidligere versjon",
    "i en eldre versjon",
    "i en tidligere publisert versjon",
    "nrk retter",
    "nrk har rettet",
    "nrk korrigerer",
    "nrk beklager",
    "rettelse:",
    "korrigering:",
    "endringen er gjort",
    "vi har rettet",
    "artikkelen er oppdatert",
    "tidligere skrev vi",
]

# Load existing corrections
if os.path.exists(DATA_FILE):
    with open(DATA_FILE) as f:
        corrections = json.load(f)
else:
    corrections = []

existing_urls = {c["url"] for c in corrections}
new_count = 0


def has_trigger(text):
    t = text.lower()
    return any(phrase in t for phrase in TRIGGERS)


def extract_correction_blocks(soup):
    blocks = []
    for el in soup.find_all(["p", "aside", "div", "blockquote"]):
        # Skip large containers that would just be the whole article
        if len(el.find_all(["p", "div"])) > 3:
            continue
        pt = el.get_text(strip=True)
        if has_trigger(pt):
            blocks.append(pt[:600])
    return " | ".join(blocks) if blocks else "Korrigert (detaljer i artikkelen)"


def check_article(url, title="", pub_date="", source="rss"):
    global new_count
    if url in existing_urls:
        return
    print(f"Checking {url}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        if not has_trigger(soup.get_text()):
            return

        correction_block = extract_correction_blocks(soup)
        date_str = pub_date or datetime.now(timezone.utc).isoformat()

        corrections.append({
            "id": int(time.time() * 1000),
            "date": date_str,
            "title": title or url,
            "what": "Feil i tidligere versjon (automatisk oppdaget)",
            "correction": correction_block,
            "url": url,
            "auto": True,
            "source": source,
        })
        existing_urls.add(url)
        new_count += 1
        print(f"  -> Rettelse funnet: {title or url}")
    except Exception as e:
        print(f"  Feil ved {url}: {e}")


# --- RSS scan ---
print("=== RSS-feeds ===")
rss_urls = []
for feed_url in RSS_FEEDS:
    feed = feedparser.parse(feed_url)
    for entry in feed.entries:
        if entry.link not in existing_urls and entry.link not in rss_urls:
            rss_urls.append((entry.link, entry.get("title", ""), entry.get("published", "")))

for url, title, pub_date in rss_urls:
    check_article(url, title=title, pub_date=pub_date, source="rss")
    time.sleep(1.0)


# --- Sitemap scan (recently modified articles = possible silent corrections) ---
print("\n=== Sitemap-skanning (siste 7 dager) ===")

def get_sitemap_urls(days_back=7, max_urls=200):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    try:
        index_xml = requests.get("https://www.nrk.no/sitemap.xml", headers=HEADERS, timeout=15).text
        root = ET.fromstring(index_xml)
    except Exception as e:
        print(f"Kunne ikke laste sitemap-indeks: {e}")
        return []

    recent_sitemaps = []
    for sitemap in root.findall("sm:sitemap", ns):
        lastmod_text = sitemap.findtext("sm:lastmod", namespaces=ns)
        loc = sitemap.findtext("sm:loc", namespaces=ns)
        if lastmod_text and loc:
            try:
                lm = datetime.fromisoformat(lastmod_text.replace("Z", "+00:00"))
                if lm > cutoff:
                    recent_sitemaps.append(loc)
            except ValueError:
                pass

    print(f"Fant {len(recent_sitemaps)} nylig oppdaterte under-sitemaps")

    urls = []
    for sm_url in recent_sitemaps[:20]:
        try:
            sm_xml = requests.get(sm_url, headers=HEADERS, timeout=15).text
            sm_root = ET.fromstring(sm_xml)
            for url_el in sm_root.findall("sm:url", ns):
                lastmod_text = url_el.findtext("sm:lastmod", namespaces=ns)
                loc = url_el.findtext("sm:loc", namespaces=ns)
                if loc and lastmod_text and loc not in existing_urls:
                    try:
                        lm = datetime.fromisoformat(lastmod_text.replace("Z", "+00:00"))
                        if lm > cutoff:
                            urls.append(loc)
                    except ValueError:
                        pass
        except Exception as e:
            print(f"Feil ved {sm_url}: {e}")
        if len(urls) >= max_urls:
            break
        time.sleep(0.5)

    return urls[:max_urls]


sitemap_urls = get_sitemap_urls()
print(f"Sjekker {len(sitemap_urls)} artikler fra sitemap")
for url in sitemap_urls:
    check_article(url, source="sitemap")
    time.sleep(1.0)


# Save
with open(DATA_FILE, "w", encoding="utf-8") as f:
    json.dump(corrections, f, ensure_ascii=False, indent=2)

print(f"\nFerdig. {new_count} nye rettelser. Totalt: {len(corrections)}")
