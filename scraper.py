import feedparser
import requests
from bs4 import BeautifulSoup
import json
import time
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

DATA_FILE = "data/corrections_raw.json"
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

# Nav/boilerplate text that signals we've matched the wrong element
NAV_NOISE = ("hopp til innhold", "nrk tv", "nrk radio", "nrk super", "nrk p3")

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


def is_nav_noise(text):
    t = text.lower()
    return any(t.startswith(prefix) for prefix in NAV_NOISE)


def extract_correction_blocks(soup):
    """
    Return the correction text(s) found in the article, or None if nothing
    clean could be extracted.

    Strategy:
      1. Prefer <p> tags (focused, rarely contain nav junk).
         Accept only if text is short enough to be a correction note (≤ 800 chars).
      2. If no <p> match, try <aside> / <blockquote> / short <div>s (≤ 500 chars).
      3. Skip anything that starts with NRK navigation boilerplate.
      4. Return None if nothing usable found — caller should skip the entry.
    """
    blocks = []

    # Pass 1: <p> elements
    for el in soup.find_all("p"):
        text = el.get_text(strip=True)
        if not text or len(text) > 800:
            continue
        if is_nav_noise(text):
            continue
        if has_trigger(text):
            blocks.append(text[:700])

    # Pass 2: other semantic elements (no large containers)
    if not blocks:
        for el in soup.find_all(["aside", "blockquote"]):
            text = el.get_text(strip=True)
            if not text or len(text) > 500:
                continue
            if is_nav_noise(text):
                continue
            if has_trigger(text):
                blocks.append(text[:700])

    # Pass 3: small <div>s as a last resort
    if not blocks:
        for el in soup.find_all("div"):
            # Must be a leaf-ish element — no child <p> or <div> blocks inside
            if el.find(["p", "div"]):
                continue
            text = el.get_text(strip=True)
            if not text or len(text) > 400:
                continue
            if is_nav_noise(text):
                continue
            if has_trigger(text):
                blocks.append(text[:700])

    return " | ".join(blocks) if blocks else None


def extract_page_title(soup):
    """Extract the article headline from the page."""
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)[:200]
    title_el = soup.find("title")
    if title_el:
        t = title_el.get_text(strip=True)
        for suffix in [" – NRK", " - NRK", " | NRK"]:
            if t.endswith(suffix):
                t = t[: -len(suffix)]
        return t.strip()[:200]
    return ""


def extract_pub_date(soup):
    """Extract the publication date from a <time datetime=\"...\"> element."""
    time_el = soup.find("time", attrs={"datetime": True})
    if time_el:
        return time_el.get("datetime", "")
    return ""


def check_article(url, title="", pub_date="", source="rss"):
    global new_count
    if url in existing_urls:
        return
    print(f"Checking {url}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")

        # Quick full-page check before doing detailed parsing
        if not has_trigger(soup.get_text()):
            return

        correction_block = extract_correction_blocks(soup)
        if correction_block is None:
            print(f"  -> Trigger funnet men ingen ren rettelsestekst. Hopper over.")
            return

        # Fill in title and date from the page if not supplied (sitemap entries)
        if not title:
            title = extract_page_title(soup) or url
        if not pub_date:
            pub_date = extract_pub_date(soup) or datetime.now(timezone.utc).isoformat()

        corrections.append({
            "id": int(time.time() * 1000),
            "date": pub_date,
            "title": title,
            "what": "Feil i tidligere versjon (automatisk oppdaget)",
            "correction_text_raw": correction_block,
            "correction": correction_block,
            "url": url,
            "auto": True,
            "source": source,
            "qa_status": "pending",
        })
        existing_urls.add(url)
        new_count += 1
        print(f"  -> Rettelse funnet: {title}")
    except Exception as e:
        print(f"  Feil ved {url}: {e}")


# ---------------------------------------------------------------------------
# RSS scan
# ---------------------------------------------------------------------------
print("=== RSS-feeds ===")
rss_urls = []
seen_in_rss = set()
for feed_url in RSS_FEEDS:
    try:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries:
            if entry.link not in existing_urls and entry.link not in seen_in_rss:
                rss_urls.append((entry.link, entry.get("title", ""), entry.get("published", "")))
                seen_in_rss.add(entry.link)
    except Exception as e:
        print(f"Feil ved feed {feed_url}: {e}")

print(f"Sjekker {len(rss_urls)} artikler fra RSS")
for url, title, pub_date in rss_urls:
    check_article(url, title=title, pub_date=pub_date, source="rss")
    time.sleep(1.0)


# ---------------------------------------------------------------------------
# Sitemap scan — recently modified articles may have silent corrections
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Save — write to corrections_raw.json
# ---------------------------------------------------------------------------
with open(DATA_FILE, "w", encoding="utf-8") as f:
    json.dump(corrections, f, ensure_ascii=False, indent=2)

print(f"\nFerdig. {new_count} nye rettelser. Totalt: {len(corrections)}")
