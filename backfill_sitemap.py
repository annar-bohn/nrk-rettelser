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
import re
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
FAILED_URLS_FILE = "data/failed_urls.json"
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
    # Merged regions + content sections
    "/vestfoldogtelemark/", "/tromsogfinnmark/", "/vestfold/",
    "/sorlandet/", "/osloogviken/", "/ostlandssendingen/",
    "/viten/", "/dokumentar/", "/klima/",
)


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
    """Three-pass extraction — runs all passes and deduplicates.

    Pass 1: <p> tags (most common, clean text)
    Pass 2: <aside>/<blockquote> (fact-box corrections, often longer/richer)
    Pass 3: Leaf <div> elements (last resort)

    All passes always run so we capture both short trigger paragraphs
    AND longer fact-box corrections from the same article.
    """
    blocks = []
    for el in soup.find_all("p"):
        text = el.get_text(strip=True)
        if not text or len(text) > 2000 or is_nav_noise(text):
            continue
        if has_trigger(text):
            blocks.append(text[:2000])
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


def load_failed_urls():
    if os.path.exists(FAILED_URLS_FILE):
        with open(FAILED_URLS_FILE) as f:
            return json.load(f)
    return []


def save_failed_urls(failed_urls):
    with open(FAILED_URLS_FILE, "w", encoding="utf-8") as f:
        json.dump(failed_urls, f, ensure_ascii=False, indent=2)


def fetch_with_retry(url, max_retries=2):
    """Fetch URL with retry on transient errors (403, 429, 5xx, timeout)."""
    for attempt in range(max_retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code in (403, 429) and attempt < max_retries:
                wait = 3 * (2 ** attempt)  # 3s, 6s
                print(f"    [{r.status_code}] Retry {attempt + 1}/{max_retries} in {wait}s: {url[:70]}")
                time.sleep(wait)
                continue
            if r.status_code >= 500 and attempt < max_retries:
                time.sleep(3)
                continue
            return r
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt < max_retries:
                time.sleep(3)
                continue
            raise
    return r  # return last response even if bad status


def process_article(url, corrections, existing_urls):
    """Fetch and process a single article URL. Returns True if a correction was found."""
    r = fetch_with_retry(url)
    if r.status_code != 200:
        print(f"    SKIP [{r.status_code}]: {url[:80]}")
        return False

    soup = BeautifulSoup(r.text, "html.parser")

    if not has_trigger(soup.get_text()):
        return False

    correction_block = extract_correction_blocks(soup)
    if correction_block is None:
        return False

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
    print(f"  -> Rettelse: {title[:70]}")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=730, help="Look back N days (default 730)")
    parser.add_argument("--max-sitemaps", type=int, default=500, help="Max sitemaps to process")
    parser.add_argument("--max-minutes", type=int, default=240, help="Time budget in minutes (default 240)")
    args = parser.parse_args()
    start_time = time.time()

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
    failed_urls = load_failed_urls()

    print(f"Loaded {len(corrections)} existing entries.")
    print(f"Already completed {len(completed_sitemaps)} sitemaps.")
    print(f"Cutoff: {cutoff.isoformat()[:10]} ({args.days} days back)")

    total_checked = 0
    total_new = 0
    total_errors = 0

    # --- Phase 1: Retry previously failed URLs ---
    now = datetime.now(timezone.utc)
    retry_candidates = [
        f for f in failed_urls
        if f.get("retry_count", 0) < 5
        and f["url"] not in existing_urls
    ]
    still_failed = [f for f in failed_urls if f not in retry_candidates]

    if retry_candidates:
        print(f"\nRetrying {len(retry_candidates)} previously failed URLs...")
        for entry in retry_candidates:
            try:
                found = process_article(entry["url"], corrections, existing_urls)
                if found:
                    total_new += 1
                total_checked += 1
                # Success — don't add back to failed list
            except Exception as e:
                entry["retry_count"] = entry.get("retry_count", 0) + 1
                entry["last_error"] = str(e)
                entry["last_retry"] = now.isoformat()
                still_failed.append(entry)
                print(f"  FAIL [{type(e).__name__}]: {entry['url'][:70]} — {e}")
                total_errors += 1
                total_checked += 1
            time.sleep(0.5)

        failed_urls = still_failed
        save_failed_urls(failed_urls)
        # Save corrections after retry phase
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(corrections, f, ensure_ascii=False, indent=2)
        print(f"Retry phase done: {len(retry_candidates) - len(still_failed) + len([f for f in failed_urls if f in still_failed])} resolved")

    # --- Phase 2: Process new sitemaps ---
    try:
        r = requests.get("https://www.nrk.no/sitemap.xml", headers=HEADERS, timeout=15)
        root = ET.fromstring(r.text)
    except Exception as e:
        print(f"Could not fetch sitemap index: {e}")
        save_failed_urls(failed_urls)
        return

    all_sitemaps = []
    for sm in root.findall("sm:sitemap", ns):
        loc = sm.findtext("sm:loc", namespaces=ns)
        if loc and loc not in completed_sitemaps:
            all_sitemaps.append(loc)

    print(f"\nSitemaps to process: {len(all_sitemaps)} (skipping {len(completed_sitemaps)} done)")

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
        sm_errors = 0

        for url in urls_to_check:
            try:
                found = process_article(url, corrections, existing_urls)
                if found:
                    sm_new += 1
                    total_new += 1
                total_checked += 1
            except Exception as e:
                failed_urls.append({
                    "url": url,
                    "error": str(e),
                    "timestamp": now.isoformat(),
                    "retry_count": 0,
                })
                print(f"  FAIL [{type(e).__name__}]: {url[:70]} — {e}")
                sm_errors += 1
                total_errors += 1
                total_checked += 1

            time.sleep(0.5)  # Be polite to NRK

        # Save progress after each sitemap
        completed_sitemaps.add(sm_url)
        progress["completed_sitemaps"] = list(completed_sitemaps)
        save_progress(progress)
        save_failed_urls(failed_urls)

        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(corrections, f, ensure_ascii=False, indent=2)

        error_note = f", {sm_errors} errors" if sm_errors else ""
        print(f"  Done: {sm_new} new corrections{error_note}, {total_checked} total checked so far")

        elapsed_min = (time.time() - start_time) / 60
        if elapsed_min > args.max_minutes:
            print(f"\nTime budget reached ({elapsed_min:.0f} min > {args.max_minutes} min). Progress saved, will resume next run.")
            break

    print(f"\n{'=' * 60}")
    print(f"Sitemap backfill complete!")
    print(f"Checked: {total_checked} articles")
    print(f"New corrections: {total_new}")
    print(f"Errors: {total_errors}")
    print(f"Failed URLs pending retry: {len(failed_urls)}")
    print(f"Total entries: {len(corrections)}")


if __name__ == "__main__":
    main()
