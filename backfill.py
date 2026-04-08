"""
backfill.py -- One-shot historical backfill for NRK Rettelser.

Searches NRK for each trigger phrase, paginates through all results,
visits each article, and extracts correction text into corrections.json.
Run once via GitHub Actions (see backfill.yml) or locally.
"""

import re
import requests
from bs4 import BeautifulSoup
import json
import time
import os
import urllib.parse
from datetime import datetime, timezone

DATA_FILE = "data/corrections.json"
os.makedirs("data", exist_ok=True)

HEADERS = {"User-Agent": "NRK-Rettelser-Backfill/1.0 (+https://github.com/annar-bohn/nrk-rettelser)"}

# Search terms, ordered from most specific (purest corrections) to broader
# Each entry: (search_query, is_standalone_correction_article)
# is_standalone=True means the whole article IS the correction (e.g. "NRK retter" articles)
SEARCH_TERMS = [
    ("nrk retter",              True),
    ("rettelse:",               False),
    ("nrk beklager",            False),
    ("nrk korrigerer",          False),
    ("i en tidligere versjon",  False),
    ("i en eldre versjon",      False),
    ("vi har rettet",           False),
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

NAV_NOISE = ("hopp til innhold", "nrk tv", "nrk radio", "nrk super", "nrk p3")

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

MAX_PAGES_PER_TERM = 50  # 50 pages * 20 results = up to 1000 articles per term


# Load existing corrections
if os.path.exists(DATA_FILE):
    with open(DATA_FILE) as f:
        corrections = json.load(f)
else:
    corrections = []

existing_urls = {c["url"] for c in corrections}
new_count = 0


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
    blocks = []
    for el in soup.find_all("p"):
        text = el.get_text(strip=True)
        if not text or len(text) > 800:
            continue
        if is_nav_noise(text):
            continue
        if has_trigger(text):
            blocks.append(text[:700])
    if not blocks:
        for el in soup.find_all(["aside", "blockquote"]):
            text = el.get_text(strip=True)
            if not text or len(text) > 500:
                continue
            if is_nav_noise(text):
                continue
            if has_trigger(text):
                blocks.append(text[:700])
    if not blocks:
        for el in soup.find_all("div"):
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


def extract_standalone_correction(soup):
    """For dedicated NRK retter articles -- the whole article IS the correction."""
    container = soup.find("article") or soup.find("main")
    if not container:
        return None
    paragraphs = []
    for p in container.find_all("p"):
        text = p.get_text(strip=True)
        if text and len(text) > 20 and not is_nav_noise(text):
            paragraphs.append(text)
    combined = " ".join(paragraphs[:6])[:800]
    return combined if combined else None


def extract_page_title(soup):
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)[:200]
    title_el = soup.find("title")
    if title_el:
        t = title_el.get_text(strip=True)
        for suffix in [" - NRK", " | NRK"]:
            if t.endswith(suffix):
                t = t[: -len(suffix)]
        return t.strip()[:200]
    return ""


def extract_pub_date(soup):
    time_el = soup.find("time", attrs={"datetime": True})
    if time_el:
        return time_el.get("datetime", "")
    return ""


def get_search_page(query, offset=0):
    encoded = urllib.parse.quote('"' + query + '"')
    url = "https://www.nrk.no/sok/?q=" + encoded + "&scope=nrkno&from=" + str(offset)
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
        print(f"  Feil ved henting av soekeside: {e}")
        return [], False


def process_article(url, is_standalone):
    global new_count
    if url in existing_urls:
        return False
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        if is_standalone:
            correction_block = extract_standalone_correction(soup)
        else:
            if not has_trigger(soup.get_text()):
                return False
            correction_block = extract_correction_blocks(soup)
        if not correction_block:
            return False
        title = extract_page_title(soup) or url
        pub_date = extract_pub_date(soup) or datetime.now(timezone.utc).isoformat()
        corrections.append({
            "id": int(time.time() * 1000),
            "date": pub_date,
            "title": title,
            "what": "Feil i tidligere versjon (automatisk oppdaget)",
            "correction": correction_block,
            "url": url,
            "auto": True,
            "source": "search_backfill",
        })
        existing_urls.add(url)
        new_count += 1
        print(f"  -> Rettelse: {title[:70]}")
        return True
    except Exception as e:
        print(f"  Feil ved {url}: {e}")
        return False


for term, is_standalone in SEARCH_TERMS:
    print("\n" + "="*60)
    print(f"Soeker: \"{term}\" (standalone={is_standalone})")
    print("="*60)
    offset = 0
    page_num = 1
    term_checked = 0

    while page_num <= MAX_PAGES_PER_TERM:
        print(f"  Side {page_num} (from={offset})...")
        urls, has_next = get_search_page(term, offset)
        print(f"  {len(urls)} nye URL-er aa sjekke")
        for url in urls:
            process_article(url, is_standalone=is_standalone)
            time.sleep(1.0)
        term_checked += len(urls)
        if not has_next:
            print("  Ingen flere sider.")
            break
        offset += 20
        page_num += 1
        time.sleep(1.5)

    print(f"  Ferdig med \"{term}\": sjekket {term_checked} artikler")
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(corrections, f, ensure_ascii=False, indent=2)
    print(f"  Lagret. {new_count} nye totalt, {len(corrections)} i filen.")

print(f"\nBackfill ferdig! {new_count} nye rettelser. Totalt: {len(corrections)}")
