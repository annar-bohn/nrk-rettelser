"""
Backfill via full sitemap crawl — finds corrections in articles modified
in the last N days (default 730 = 2 years). Unlike backfill2.py which relies
on NRK's search engine, this checks every article directly, catching
corrections hidden in fact-boxes and non-standard markup.

Designed to run as a GitHub Action (takes hours). Saves progress after
each sitemap so it can be resumed if interrupted.

Usage:
  python3 backfill_sitemap.py [--days 730] [--max-sitemaps 500]
"""

import json
import time
import os
import sys
import argparse
import xml.etree.ElementTree as ET
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta

DATA_FILE = "data/corrections_raw.json"
PROGRESS_FILE = "data/sitemap_progress.json"
os.makedirs("data", exist_ok=True)

HEADERS = {"User-Agent": "NRK-Rettelser-Backfill/2.0 (+https://github.com/annar-bohn/nrk-rettelser)"}

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
)


def has_trigger(text):
    t = text.lower()
    return any(phrase in t for phrase in TRIGGERS)


def is_nav_noise(text):
    t = text.lower()
    return any(t.startswith(prefix) for prefix in NAV_NOISE)


def extract_correction_blocks(soup):
    """Three-pass extraction matching scraper.py logic."""
    blocks = []
    for el in soup.find_all("p"):
        text = el.get_text(strip=True)
        if not text or len(text) > 800 or is_nav_noise(text):
            continue
        if has_trigger(text):
            blocks.append(text[:2000])
    if not blocks:
        for el in soup.find_all(["aside", "blockquote"]):
            text = el.get_text(strip=True)
            if not text or len(text) > 2000 or is_nav_noise(text):
                continue
            if has_trigger(text):
                blocks.append(text[:2000])
    if not blocks:
        for el in soup.find_all("div"):
            if el.find(["p", "div"]):
                continue
            text = el.get_text(strip=True)
            if not text or len(text) > 800 or is_nav_noise(text):
                continue
            if has_trigger(text):
                blocks.append(text[:2000])
    return " | ".join(blocks) if blocks else None


def extract_page_title(soup):
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
    time_el = soup.find("time", attrs={"datetime": True})
    if time_el:
        return time_el.get("datetime", "")
    return ""


def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"completed_sitemaps": []}


def save_progress(progress):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=730, help="Look back N days (default 730)")
    parser.add_argument("--max-sitemaps", type=int, default=500, help="Max sitemaps to process")
    args = parser.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    # Load existing data
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            corrections = json.load(f)
    else:
        corrections = []

    existing_urls = {c["url"] for c in corrections}
    progress = load_progress()
    completed_sitemaps = set(progress.get("completed_sitemaps", []))

    print(f"Loaded {len(corrections)} existing entries.")
    print(f"Already completed {len(completed_sitemaps)} sitemaps.")
    print(f"Cutoff: {cutoff.isoformat()[:10]} ({args.days} days back)")

    # Fetch sitemap index
    try:
        r = requests.get("https://www.nrk.no/sitemap.xml", headers=HEADERS, timeout=15)
        root = ET.fromstring(r.text)
    except Exception as e:
        print(f"Could not fetch sitemap index: {e}")
        return

    all_sitemaps = []
    for sm in root.findall("sm:sitemap", ns):
        loc = sm.findtext("sm:loc", namespaces=ns)
        if loc and loc not in completed_sitemaps:
            all_sitemaps.append(loc)

    print(f"Sitemaps to process: {len(all_sitemaps)} (skipping {len(completed_sitemaps)} done)")

    total_checked = 0
    total_new = 0

    for sm_idx, sm_url in enumerate(all_sitemaps[: args.max_sitemaps]):
        print(f"\n[{sm_idx + 1}/{min(len(all_sitemaps), args.max_sitemaps)}] {sm_url}")

        try:
            r = requests.get(sm_url, headers=HEADERS, timeout=15)
            sm_root = ET.fromstring(r.text)
        except Exception as e:
            print(f"  Error fetching sitemap: {e}")
            continue

        # Filter URLs by lastmod and section
        urls_to_check = []
        for url_el in sm_root.findall("sm:url", ns):
            loc = url_el.findtext("sm:loc", namespaces=ns)
            lastmod = url_el.findtext("sm:lastmod", namespaces=ns)

            if not loc or loc in existing_urls:
                continue
            if not any(sec in loc for sec in ARTICLE_SECTIONS):
                continue
            if lastmod:
                try:
                    lm = datetime.fromisoformat(lastmod.replace("Z", "+00:00"))
                    if lm < cutoff:
                        continue
                except ValueError:
                    pass

            urls_to_check.append(loc)

        print(f"  {len(urls_to_check)} URLs to check (after filters)")
        sm_new = 0

        for url in urls_to_check:
            try:
                r = requests.get(url, headers=HEADERS, timeout=10)
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.text, "html.parser")

                if not has_trigger(soup.get_text()):
                    total_checked += 1
                    continue

                correction_block = extract_correction_blocks(soup)
                if correction_block is None:
                    total_checked += 1
                    continue

                title = extract_page_title(soup) or url
                pub_date = extract_pub_date(soup) or ""

                corrections.append({
                    "id": int(time.time() * 1000),
                    "date": pub_date,
                    "title": title,
                    "what": "Feil i tidligere versjon (automatisk oppdaget)",
                    "correction_text_raw": correction_block,
                    "correction": correction_block,
                    "url": url,
                    "auto": True,
                    "source": "sitemap_backfill",
                    "qa_status": "pending",
                })
                existing_urls.add(url)
                sm_new += 1
                total_new += 1
                total_checked += 1
                print(f"  -> Rettelse: {title[:70]}")

            except Exception as e:
                total_checked += 1
                continue

            time.sleep(0.5)  # Be polite to NRK

        # Save progress after each sitemap
        completed_sitemaps.add(sm_url)
        progress["completed_sitemaps"] = list(completed_sitemaps)
        save_progress(progress)

        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(corrections, f, ensure_ascii=False, indent=2)

        print(f"  Done: {sm_new} new corrections, {total_checked} total checked so far")

    print(f"\n{'=' * 60}")
    print(f"Sitemap backfill complete!")
    print(f"Checked: {total_checked} articles")
    print(f"New corrections: {total_new}")
    print(f"Total entries: {len(corrections)}")


if __name__ == "__main__":
    main()
