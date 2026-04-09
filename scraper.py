"""
NRK Rettelser – ongoing scraper.
Runs every 6 hours via GitHub Actions.

Strategy:
  1. Search-based scan: queries NRK search for correction trigger phrases,
     checks first 2 pages per term. Fast (~30 sec), catches most corrections.
  2. Sitemap scan: checks articles modified in the last 30 days.
     Catches corrections in fact-boxes and non-standard markup that NRK search
     doesn't index. Slower (~5–15 min depending on volume).

For deep historical backfills, use backfill_sitemap.py instead.
"""

import re
import requests
from bs4 import BeautifulSoup
import json
import time
import os
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

DATA_FILE = "data/corrections_raw.json"
os.makedirs("data", exist_ok=True)

HEADERS = {"User-Agent": "NRK-Rettelser-Bot/3.0 (+https://github.com/annar-bohn/nrk-rettelser)"}

TRIGGERS = [
    "i en tidligere versjon",
    "i en eldre versjon",
    "i en tidligere publisert versjon",
    "nrk retter",
    "nrk har rettet",
    "nrk korrigerer",
    "nrk beklager",
    "rettelse:",
    "rettelse",
    "retting:",
    "retting",
    "korrigering:",
    "presisering:",
    "endringen er gjort",
    "endringane er gjort",
    "endringane vart gjort",
    "det er gjort endringar",
    "vi har rettet",
    "artikkelen er oppdatert",
    "artikkelen er endra",
    "tidligere skrev vi",
    "etter publisering",
]

# Search terms to query NRK's search engine
SEARCH_TERMS = [
    "rettelse:",
    "retting:",
    "retting",
    "presisering:",
    "etter publisering",
    "endringane vart gjort",
    "det er gjort endringar",
    "artikkelen er endra",
    "i en tidligere versjon",
    "nrk beklager",
]

# How many search result pages to check per term (20 results per page)
MAX_SEARCH_PAGES = 2

ARTICLE_SECTIONS = (
    "/nyheter/", "/sport/", "/kultur/", "/urix/", "/norge/",
    "/nordland/", "/vestland/", "/rogaland/", "/innlandet/",
    "/trondelag/", "/troms/", "/finnmark/", "/ostfold/",
    "/buskerud/", "/telemark/", "/agder/", "/mr/", "/sognogfjordane/",
    "/hordaland/", "/stfold/", "/akershus/", "/stor-oslo/",
    "/ytring/", "/nyttig/", "/livsstil/", "/sapmi/",
    # Merged regions + content sections
    "/vestfoldogtelemark/", "/tromsogfinnmark/", "/vestfold/",
    "/sorlandet/", "/osloogviken/", "/ostlandssendingen/",
    "/viten/", "/dokumentar/", "/klima/",
)

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Word-boundary regex for bare single-word triggers to avoid matching
# compound words like "henrettelse", "opprettelse", "feilretting"
BARE_TRIGGERS_RE = re.compile(r'\b(rettelse|retting)\b', re.IGNORECASE)


def has_trigger(text):
    t = text.lower()
    for phrase in TRIGGERS:
        if phrase in ("rettelse", "retting"):
            continue  # handled by BARE_TRIGGERS_RE below
        if phrase in t:
            return True
    return bool(BARE_TRIGGERS_RE.search(t))


def is_nav_noise(text):
    t = text.lower()
    return any(t.startswith(prefix) for prefix in NAV_NOISE)


def extract_correction_blocks(soup):
    """
    Return the correction text(s) found in the article, or None if nothing
    clean could be extracted.

    All passes always run so we capture both short trigger paragraphs
    AND longer fact-box corrections from the same article.

    Pass 1: <p> tags (most common, clean text)
    Pass 2: <aside>/<blockquote> (fact-box corrections, often longer/richer)
    Pass 3: Leaf <div> elements (last resort, only if passes 1-2 found nothing)
    """
    blocks = []

    # Pass 1: <p> elements
    for el in soup.find_all("p"):
        text = el.get_text(strip=True)
        if not text or len(text) > 2000 or is_nav_noise(text):
            continue
        if has_trigger(text):
            blocks.append(text[:2000])

    # Pass 2: semantic elements like <aside> fact-boxes and <blockquote>
    for el in soup.find_all(["aside", "blockquote"]):
        text = el.get_text(strip=True)
        if not text or len(text) > 2000 or is_nav_noise(text):
            continue
        if has_trigger(text):
            blocks.append(text[:2000])

    # Pass 3: small <div>s as a last resort
    if not blocks:
        for el in soup.find_all("div"):
            if el.find(["p", "div"]):
                continue
            text = el.get_text(strip=True)
            if not text or len(text) > 2000 or is_nav_noise(text):
                continue
            if has_trigger(text):
                blocks.append(text[:2000])

    # Deduplicate: remove blocks that are substrings of longer blocks
    if len(blocks) > 1:
        blocks.sort(key=len, reverse=True)
        deduped = []
        for b in blocks:
            if not any(b in existing for existing in deduped):
                deduped.append(b)
        blocks = deduped

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
    meta = soup.find("meta", property="article:published_time")
    if meta:
        return meta.get("content", "")
    return ""


def check_article(url, title="", pub_date="", source="search"):
    """Fetch an article, check for correction triggers, and add if found."""
    global new_count
    if url in existing_urls:
        return
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return
        soup = BeautifulSoup(r.text, "html.parser")

        # Quick full-page check before doing detailed parsing
        if not has_trigger(soup.get_text()):
            return

        correction_block = extract_correction_blocks(soup)
        if correction_block is None:
            return

        # Fill in title and date from the page if not supplied
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
        print(f"  -> Rettelse funnet: {title[:70]}")
    except Exception as e:
        print(f"  Feil ved {url}: {e}")


# ---------------------------------------------------------------------------
# 1. Search-based scan — query NRK search for correction phrases
# ---------------------------------------------------------------------------
def get_search_page(query, offset=0):
    """Fetch one page of NRK search results and return article URLs."""
    encoded = urllib.parse.quote(f'"{query}"')
    url = f"https://www.nrk.no/sok/?q={encoded}&scope=nrkno&from={offset}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        article_urls = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if (
                href.startswith("https://www.nrk.no/")
                and "/sok/" not in href
                and href != "https://www.nrk.no/"
                and href not in existing_urls
                and href not in seen
                and any(s in href for s in ARTICLE_SECTIONS)
            ):
                article_urls.append(href)
                seen.add(href)
        has_next = any(a.get_text(strip=True) == "Neste side" for a in soup.find_all("a"))
        return article_urls, has_next
    except Exception as e:
        print(f"  Feil ved søk: {e}")
        return [], False


print("=== Søk-basert skanning ===")
search_checked = 0
for term in SEARCH_TERMS:
    print(f"Søker: \"{term}\"")
    for page in range(MAX_SEARCH_PAGES):
        offset = page * 20
        urls, has_next = get_search_page(term, offset)
        for url in urls:
            check_article(url, source="search")
            search_checked += 1
            time.sleep(0.5)
        if not has_next:
            break
        time.sleep(0.5)

print(f"Søk ferdig. Sjekket {search_checked} artikler.\n")


# ---------------------------------------------------------------------------
# 2. Sitemap scan — recently modified articles (last 30 days)
# ---------------------------------------------------------------------------
print("=== Sitemap-skanning (siste 30 dager) ===")


def get_sitemap_urls(days_back=30, max_urls=1000):
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
    for sm_url in recent_sitemaps[:50]:
        try:
            sm_xml = requests.get(sm_url, headers=HEADERS, timeout=15).text
            sm_root = ET.fromstring(sm_xml)
            for url_el in sm_root.findall("sm:url", ns):
                lastmod_text = url_el.findtext("sm:lastmod", namespaces=ns)
                loc = url_el.findtext("sm:loc", namespaces=ns)
                if loc and lastmod_text and loc not in existing_urls:
                    if not any(s in loc for s in ARTICLE_SECTIONS):
                        continue
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
    time.sleep(0.5)


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------
with open(DATA_FILE, "w", encoding="utf-8") as f:
    json.dump(corrections, f, ensure_ascii=False, indent=2)

print(f"\nFerdig. {new_count} nye rettelser. Totalt: {len(corrections)}")
