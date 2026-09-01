"""
SVT Rättelser – historical audit.
Phase 6 of the SVT expansion plan (.claude/plans/svt.md §5 Phase 6).

Why this exists: svt_collect.py can only see the ~48h window of SVT's news
sitemap, and SVT publishes no sitemap archive and has no site search. So
anything older than that window is invisible to the ongoing collector.

Strategy: discover historical article URLs from the Internet Archive's CDX
index, then fetch each one LIVE. Fetching live (not the archived snapshot) is
the point — corrections are appended to an article after publication, so the
current page is what carries them, while the archive is only used as a
catalogue of URLs that ever existed.

Extraction is imported from svt_collect.py rather than copied. That module
guards its entry point with __main__, so importing it runs nothing.

Progress is written after every batch, so a run killed mid-flight resumes
where it stopped — the same pattern as backfill_sitemap.py.

Usage:
  python3 svt_audit.py --from 2020 --to 2026 --max-urls 500
  python3 svt_audit.py --max-urls 2000 --max-minutes 240   # unattended
  python3 svt_audit.py --stats                             # progress only
"""

import argparse
import json
import os
import time
import urllib.parse

import requests

import svt_collect as sc

CDX_URL = "http://web.archive.org/cdx/search/cdx"
PROGRESS_FILE = os.path.join(sc.DATA_DIR, "audit_progress.json")

# Path prefixes to sweep. Deliberately split per section rather than one broad
# "www.svt.se/nyheter/*": that query covers so much of the index that CDX times
# out on it. Narrower prefixes each return quickly and cover the same ground.
DEFAULT_PREFIXES = [
    "www.svt.se/nyheter/inrikes/*",
    "www.svt.se/nyheter/utrikes/*",
    "www.svt.se/nyheter/lokalt/*",
    "www.svt.se/nyheter/vetenskap/*",
    "www.svt.se/nyheter/granskning/*",
    "www.svt.se/nyheter/snabbkollen/*",
    "www.svt.se/kultur/*",
    "www.svt.se/sport/*",
    "www.svt.se/vader/*",
]

CDX_SLEEP = 2.0     # archive.org is a shared free service — query it gently
CDX_TIMEOUT = 120   # broad wildcard queries are slow even when they succeed
CDX_RETRIES = 2


def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"checked_urls": [], "cdx_cursors": {}, "stats": {}}


def save_progress(progress):
    os.makedirs(sc.DATA_DIR, exist_ok=True)
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def fetch_cdx_page(prefix, year_from, year_to, limit, offset):
    """Fetch one page of CDX results. Returns a list of original URLs."""
    params = {
        "url": prefix,
        "output": "json",
        "from": str(year_from),
        "to": str(year_to),
        "filter": ["statuscode:200", "mimetype:text/html"],
        "collapse": "urlkey",
        "fl": "original",
        "limit": str(limit),
        "offset": str(offset),
    }
    query = urllib.parse.urlencode(params, doseq=True)
    rows = None
    for attempt in range(CDX_RETRIES + 1):
        try:
            r = requests.get(f"{CDX_URL}?{query}", headers=sc.HEADERS,
                             timeout=CDX_TIMEOUT)
            if r.status_code >= 500 and attempt < CDX_RETRIES:
                # 502/504 are routine on broad wildcard queries — archive.org
                # gives up before the query finishes. Backing off usually works.
                wait = 10 * (attempt + 1)
                print(f"  CDX HTTP {r.status_code} for {prefix} — retry "
                      f"{attempt + 1}/{CDX_RETRIES} in {wait}s")
                time.sleep(wait)
                continue
            if r.status_code != 200:
                print(f"  CDX HTTP {r.status_code} for {prefix} (offset {offset})")
                return []
            rows = r.json()
            break
        except ValueError:
            # An empty result set comes back as an empty body, not valid JSON.
            return []
        except Exception as e:
            if attempt < CDX_RETRIES:
                wait = 5 * (attempt + 1)
                print(f"  CDX {type(e).__name__} for {prefix} — retry "
                      f"{attempt + 1}/{CDX_RETRIES} in {wait}s")
                time.sleep(wait)
                continue
            print(f"  CDX error for {prefix} (giving up): {e}")
            return []

    if rows is None:
        return []

    if not rows:
        return []
    # First row is the column header when fl= is echoed back.
    if rows and rows[0] and rows[0][0] == "original":
        rows = rows[1:]
    return [row[0] for row in rows if row]


def discover_urls(prefixes, year_from, year_to, page_size, progress, want,
                  deadline=None):
    """Collect candidate article URLs from CDX, skipping ones already checked.

    Walks each prefix with a persisted offset cursor so repeat runs continue
    deeper into the index instead of re-reading the same first page.

    `deadline` (a time.time() value) hard-stops discovery. Without it, CDX can
    eat the entire run: archive.org answers broad prefixes with 503/504 far
    more often under load than a one-off query suggests, and canonicalising
    away query strings collapses huge stretches of the index into a handful of
    distinct articles. The 2026-09-01 run spent four hours paging 226k rows
    for 600 candidates and then had no budget left to fetch any of them.
    """
    checked = set(progress["checked_urls"])
    known = {c["url"] for c in load_dataset()}
    cursors = progress.setdefault("cdx_cursors", {})

    candidates = []
    for prefix in prefixes:
        if len(candidates) >= want:
            break
        if deadline and time.time() > deadline:
            print("  Discovery time budget reached — fetching what we have.")
            break
        offset = cursors.get(prefix, 0)
        empty_pages = 0
        while len(candidates) < want and empty_pages < 2:
            if deadline and time.time() > deadline:
                print("  Discovery time budget reached — fetching what we have.")
                break
            print(f"  CDX {prefix} offset={offset}")
            urls = fetch_cdx_page(prefix, year_from, year_to, page_size, offset)
            if not urls:
                empty_pages += 1
                if empty_pages >= 2:
                    print(f"  {prefix}: index exhausted for {year_from}–{year_to}")
                break
            offset += len(urls)
            cursors[prefix] = offset

            for raw in urls:
                url = sc.canonical_url(raw.strip())
                if not url.startswith("https://"):
                    url = url.replace("http://", "https://", 1)
                skip, _ = sc.should_skip(url)
                if skip or url in checked or url in known:
                    continue
                candidates.append(url)
                checked.add(url)
                if len(candidates) >= want:
                    break
            time.sleep(CDX_SLEEP)

    return candidates


def load_dataset():
    if os.path.exists(sc.DATA_FILE):
        with open(sc.DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def main():
    parser = argparse.ArgumentParser(
        description="Historical SVT correction audit via Wayback CDX discovery."
    )
    parser.add_argument("--from", dest="year_from", type=int, default=2020,
                        help="Earliest archive year to sweep (default 2020)")
    parser.add_argument("--to", dest="year_to", type=int, default=2026,
                        help="Latest archive year to sweep (default 2026)")
    parser.add_argument("--max-urls", type=int, default=500,
                        help="Max live article fetches this run (default 500)")
    parser.add_argument("--page-size", type=int, default=1000,
                        help="CDX rows per request (default 1000)")
    parser.add_argument("--max-minutes", type=int, default=240,
                        help="Time budget in minutes (default 240)")
    parser.add_argument("--stats", action="store_true",
                        help="Print progress state and exit")
    args = parser.parse_args()

    progress = load_progress()

    if args.stats:
        st = progress.get("stats", {})
        print(f"URLs checked so far:    {len(progress['checked_urls'])}")
        print(f"Corrections found:      {st.get('found_total', 0)}")
        print(f"Runs completed:         {st.get('runs', 0)}")
        print(f"CDX cursors:            {progress.get('cdx_cursors', {})}")
        print(f"Dataset size:           {len(load_dataset())}")
        return

    start = time.time()
    corrections = load_dataset()
    existing_urls = {c["url"] for c in corrections}
    initial = len(corrections)

    print("=== SVT historical audit ===")
    print(f"Archive years:  {args.year_from}–{args.year_to}")
    print(f"Already checked: {len(progress['checked_urls'])} URLs")
    print(f"Dataset:         {initial} entries\n")

    # Candidates discovered but never fetched (a previous run ran out of time)
    # are carried over. Without this they would be lost for good: the CDX
    # cursor has already advanced past them, so re-discovery never sees them.
    candidates = [u for u in progress.get("pending_candidates", [])
                  if u not in existing_urls]
    if candidates:
        print(f"Carrying over {len(candidates)} candidates from a previous run.")

    if len(candidates) < args.max_urls:
        # Discovery gets a bounded slice of the budget; the rest is for
        # fetching, which is the part that actually finds corrections.
        discovery_deadline = time.time() + args.max_minutes * 60 * 0.3
        print("Discovering candidate URLs from the Wayback CDX index...")
        candidates += discover_urls(
            DEFAULT_PREFIXES, args.year_from, args.year_to,
            args.page_size, progress, args.max_urls - len(candidates),
            deadline=discovery_deadline,
        )

    progress["pending_candidates"] = candidates
    save_progress(progress)
    print(f"\n{len(candidates)} candidate URLs to check live\n")

    if not candidates:
        print("Nothing new to check — the index cursors may be exhausted for "
              "this year range. Widen --from/--to or reset cdx_cursors.")
        save_progress(progress)
        return

    stats = {
        "listed": 0, "skipped_filter": 0, "already_known": 0, "fetched": 0,
        "trigger_hits": 0, "extracted": 0, "compound_guard_worked": 0,
        "compound_with_real_trigger": 0, "candidate_only_precheck": 0,
        "candidate_only_no_extraction": 0, "candidate_only_extracted": 0,
    }

    checked_this_run = 0
    for url in candidates:
        elapsed_min = (time.time() - start) / 60
        if elapsed_min > args.max_minutes:
            print(f"\nTime budget reached ({elapsed_min:.0f} min). Stopping; "
                  f"progress saved and resumable.")
            break

        stats["fetched"] += 1
        checked_this_run += 1
        try:
            sc.process_article(url, "", corrections, existing_urls, stats)
        except Exception as e:
            print(f"  FAIL [{type(e).__name__}]: {url[:70]} — {e}")

        progress["checked_urls"].append(url)
        progress["pending_candidates"] = candidates[checked_this_run:]

        # Persist every 25 URLs so an interrupted run loses almost nothing.
        if checked_this_run % 25 == 0:
            save_progress(progress)
            if len(corrections) > initial:
                with open(sc.DATA_FILE, "w", encoding="utf-8") as f:
                    json.dump(corrections, f, ensure_ascii=False, indent=2)
            print(f"  … {checked_this_run}/{len(candidates)} checked, "
                  f"{len(corrections) - initial} found")

        time.sleep(sc.FETCH_SLEEP)

    found = len(corrections) - initial
    progress["pending_candidates"] = candidates[checked_this_run:]
    st = progress.setdefault("stats", {})
    st["found_total"] = st.get("found_total", 0) + found
    st["runs"] = st.get("runs", 0) + 1
    save_progress(progress)

    if found:
        with open(sc.DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(corrections, f, ensure_ascii=False, indent=2)

    hit_rate = (found / checked_this_run * 100) if checked_this_run else 0.0
    print("\n=== Audit summary ===")
    print(f"  Live articles fetched:   {checked_this_run}")
    print(f"  Trigger pre-check hits:  {stats['trigger_hits']}")
    print(f"  Corrections found:       {found}  ({hit_rate:.2f}% of fetched)")
    print(f"  Compound-word guard held on: {stats['compound_guard_worked']} pages")
    print(f"  Dataset now:             {len(corrections)} entries")
    print(f"  Total checked all runs:  {len(progress['checked_urls'])}")


if __name__ == "__main__":
    main()
