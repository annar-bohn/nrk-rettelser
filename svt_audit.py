"""
SVT Rättelser – historical audit.
Phase 6 of the SVT expansion plan (.claude/plans/svt.md §5 Phase 6).

Why this exists: svt_collect.py can only see the ~48h window of SVT's news
sitemap, and SVT publishes no sitemap archive and has no site search. So
anything older than that window is invisible to the ongoing collector.

Strategy: discover candidate article URLs, then fetch each one LIVE. Fetching
live is the point — corrections are appended to an article after publication,
so the current page is what carries them; discovery only supplies URLs.

Two discovery sources (--discovery):
  brave (default) — Brave Search API, `site:svt.se` + each trigger phrase,
      sliced by date. Goes directly at pages likely to hold a correction.
      Paid per query: needs BRAVE_API_KEY and is capped by --max-queries.
  cdx — Internet Archive CDX index, brute-force by path prefix. Kept for
      reference; the 2026-08-31 run showed discovery there eats the whole
      budget for a near-zero yield.

Extraction is imported from svt_collect.py rather than copied. That module
guards its entry point with __main__, so importing it runs nothing.

Progress is written after every batch, so a run killed mid-flight resumes
where it stopped — the same pattern as backfill_sitemap.py.

Usage:
  python3 svt_audit.py --from 2026 --to 2026 --discover-only --max-queries 60
                                                           # pilot: recall check
  python3 svt_audit.py --from 2020 --to 2026 --max-urls 500
  python3 svt_audit.py --max-urls 2000 --max-minutes 240   # unattended
  python3 svt_audit.py --stats                             # progress only
"""

import argparse
import calendar
import json
import os
import sys
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

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_PAGE_SIZE = 20   # API maximum per request
BRAVE_MAX_OFFSET = 9   # API maximum page index — so 200 results per query at most
BRAVE_SLEEP = 1.1      # stay under 1 request/second regardless of plan
BRAVE_RETRIES = 3

# Query set chosen from the 2026-10-01 recall probe (svt_brave_probe.py). No
# single query surfaces more than about a fifth of known corrections; each
# finds a different handful, so coverage comes from the union. The live fetch
# + extraction decides what is a correction — these only surface candidates.
_PHRASES = [
    '"Rättelse:"', '"Förtydligande:"', '"i en tidigare version"', "rättelse",
    '"Rättelse"', "förtydligande", '"tidigare version"',
    '"tidigare version av artikeln"', '"tidigare version av videon"',
    '"tidigare version av texten"', '"tidigare version stod"',
    '"rätt är att"', '"korrekt är att"', '"har förtydligats"',
    '"artikeln har uppdaterats"', "rättelse tidigare version",
    'inbody:"rättelse"',
]
_SECTIONS = [
    "nyheter/inrikes", "nyheter/utrikes", "nyheter/lokalt", "nyheter/lokalt/vast",
    "nyheter/lokalt/varmland", "nyheter/lokalt/skane", "sport", "kultur",
]
# Searched once per year (and month by month if a year fills all 200 results).
BRAVE_QUERIES = [f"site:svt.se {p}" for p in _PHRASES] + [
    f"site:svt.se/{sec} {w}" for sec in _SECTIONS
    for w in ("rättelse", '"tidigare version"')
]
# Always searched month by month as well: a narrower date slice makes Brave
# return different results for the same phrase, not just fewer.
BRAVE_MONTHLY_QUERIES = ['site:svt.se "Rättelse:"', "site:svt.se rättelse"]

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


def brave_search(query, freshness, offset, api_key):
    """One Brave web-search request. Returns (urls, more_available), or None
    when the request failed for good (the slice is then retried next run)."""
    params = {
        "q": query,
        "count": BRAVE_PAGE_SIZE,
        "offset": offset,
        "freshness": freshness,
        "country": "SE",
        "search_lang": "sv",
        "result_filter": "web",
        "text_decorations": "false",
    }
    headers = {"Accept": "application/json", "X-Subscription-Token": api_key}
    for attempt in range(BRAVE_RETRIES + 1):
        try:
            r = requests.get(BRAVE_URL, params=params, headers=headers, timeout=30)
        except requests.exceptions.RequestException as e:
            print(f"  Brave {type(e).__name__} — attempt {attempt + 1}")
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 200:
            data = r.json()
            results = (data.get("web") or {}).get("results") or []
            more = bool((data.get("query") or {}).get("more_results_available"))
            return [x["url"] for x in results if x.get("url")], more
        if r.status_code in (429, 500, 502, 503, 504) and attempt < BRAVE_RETRIES:
            wait = 5 * (attempt + 1)
            print(f"  Brave HTTP {r.status_code} — retry in {wait}s")
            time.sleep(wait)
            continue
        # 401/403/422 etc. will not fix themselves; show why and stop retrying.
        print(f"  Brave HTTP {r.status_code}: {r.text[:300]}")
        return None
    return None


def month_slices(year):
    return [
        f"{year}-{m:02d}-01to{year}-{m:02d}-{calendar.monthrange(year, m)[1]:02d}"
        for m in range(1, 13)
    ]


def discover_brave(year_from, year_to, progress, want, max_queries, api_key,
                   seen_known):
    """Collect candidate URLs from Brave, newest year first.

    Each (query, date slice) is searched once and recorded in
    progress["brave_done"], so a re-run never pays for the same slice twice.
    A year slice that fills all 200 available results is truncated, not
    exhausted — it is re-searched month by month instead.

    Candidates and progress are saved after every slice: each request costs
    money, so nothing discovered may live only in memory.

    `seen_known` collects result URLs already in the dataset — the recall
    signal for a pilot over a period the sitemap collector has covered.
    """
    checked = set(progress["checked_urls"])
    known = {c["url"] for c in load_dataset()}
    done = progress.setdefault("brave_done", [])
    done_set = set(done)
    pending = progress.setdefault("pending_candidates", [])
    queued = set(pending)
    saturated_keys = progress.setdefault("brave_saturated", [])
    st = progress.setdefault("stats", {})

    # (date slice, year or None, queries to run on it)
    slices = []
    for y in range(year_to, year_from - 1, -1):
        slices.append((f"{y}-01-01to{y}-12-31", y, BRAVE_QUERIES))
        slices += [(ms, None, BRAVE_MONTHLY_QUERIES) for ms in month_slices(y)]
    # Which queries surfaced each candidate — lets the fetch step report a
    # hit rate per query, so noisy ones can be dropped on evidence.
    origin = progress.setdefault("brave_origin", {})
    used = 0
    found = 0
    while slices and found < want:
        freshness, year, queries = slices.pop(0)
        for query in queries:
            key = f"{query}|{freshness}"
            if key in done_set:
                if (key in saturated_keys and year is not None
                        and query not in BRAVE_MONTHLY_QUERIES):
                    slices += [(ms, None, [query]) for ms in month_slices(year)]
                continue
            if used >= max_queries:
                print(f"  Query budget reached ({max_queries}); resumable.")
                return found
            urls, complete, saturated = [], True, False
            for offset in range(BRAVE_MAX_OFFSET + 1):
                if used >= max_queries:
                    complete = False
                    break
                res = brave_search(query, freshness, offset, api_key)
                used += 1
                st["brave_queries_total"] = st.get("brave_queries_total", 0) + 1
                time.sleep(BRAVE_SLEEP)
                if res is None:
                    complete = False
                    break
                page, more = res
                urls += page
                if not more or not page:
                    break
                if offset == BRAVE_MAX_OFFSET:
                    saturated = True

            new = 0
            for raw in urls:
                url = sc.canonical_url(raw.strip()).replace("http://", "https://", 1)
                if url in known:
                    seen_known.add(url)
                    continue
                skip, _ = sc.should_skip(url)
                if skip or url in checked:
                    continue
                if query not in origin.setdefault(url, []):
                    origin[url].append(query)
                if url in queued:
                    continue
                pending.append(url)
                queued.add(url)
                new += 1
            found += new
            note = " SATURATED" if saturated else ""
            print(f"  {freshness}  {query}: {len(urls)} results, {new} new{note}")

            if complete:
                done.append(key)
                done_set.add(key)
                if saturated:
                    saturated_keys.append(key)
                if (saturated and year is not None
                        and query not in BRAVE_MONTHLY_QUERIES):
                    # Truncated at 200 — split this query's year into months.
                    # (Month slices carry year=None so they never split again.)
                    slices += [(ms, None, [query]) for ms in month_slices(year)]
            progress["brave_seen_known"] = sorted(seen_known)
            save_progress(progress)
    return found


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
    parser.add_argument("--discovery", choices=("brave", "cdx"), default="brave",
                        help="Where candidate URLs come from (default brave)")
    parser.add_argument("--max-queries", type=int, default=200,
                        help="Max paid Brave requests this run (default 200)")
    parser.add_argument("--discover-only", action="store_true",
                        help="Run discovery, save candidates, fetch nothing")
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
        print(f"Brave queries (total):  {st.get('brave_queries_total', 0)}")
        print(f"Brave slices done:      {len(progress.get('brave_done', []))}")
        print(f"Pending candidates:     {len(progress.get('pending_candidates', []))}")
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

    progress["pending_candidates"] = candidates
    # Persisted so the recall signal survives a pilot split across runs.
    seen_known = set(progress.get("brave_seen_known", []))
    if args.discovery == "brave" and (args.discover_only
                                      or len(candidates) < args.max_urls):
        api_key = os.environ.get("BRAVE_API_KEY", "").strip()
        if not api_key:
            # Fail loudly: a silent skip here would look like "nothing found".
            sys.exit("BRAVE_API_KEY is not set — cannot run Brave discovery.")
        print("Discovering candidate URLs from Brave Search...")
        want = args.max_urls - len(candidates)
        if args.discover_only:
            want = float("inf")
        discover_brave(args.year_from, args.year_to, progress, want,
                       args.max_queries, api_key, seen_known)
        candidates = progress["pending_candidates"]
        in_range = [c["url"] for c in corrections
                    if args.year_from <= int((c.get("date") or "0000")[:4] or 0)
                    <= args.year_to]
        hit = sum(1 for u in in_range if u in seen_known)
        print(f"\nRecall signal: {hit} of {len(in_range)} dataset entries "
              f"published {args.year_from}–{args.year_to} appeared in results "
              f"({len(seen_known)} known URLs seen in total).")
    elif len(candidates) < args.max_urls:
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

    if args.discover_only:
        print("--discover-only: candidates saved, nothing fetched.")
        return
    candidates = candidates[:args.max_urls]
    carry_over = progress["pending_candidates"][len(candidates):]

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
    origin = progress.get("brave_origin", {})
    yield_by_query = {}   # query -> [fetched, found]
    for url in candidates:
        elapsed_min = (time.time() - start) / 60
        if elapsed_min > args.max_minutes:
            print(f"\nTime budget reached ({elapsed_min:.0f} min). Stopping; "
                  f"progress saved and resumable.")
            break

        stats["fetched"] += 1
        checked_this_run += 1
        try:
            sc.process_article(url, "", corrections, existing_urls, stats,
                               source=f"audit_{args.discovery}")
        except Exception as e:
            print(f"  FAIL [{type(e).__name__}]: {url[:70]} — {e}")
        for q in origin.pop(url, []):
            tally = yield_by_query.setdefault(q, [0, 0])
            tally[0] += 1
            tally[1] += url in existing_urls

        progress["checked_urls"].append(url)
        progress["pending_candidates"] = candidates[checked_this_run:] + carry_over

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
    progress["pending_candidates"] = candidates[checked_this_run:] + carry_over
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
    if yield_by_query:
        print("\n=== Yield per query (a URL counts under every query that surfaced it) ===")
        for q, (n, hit) in sorted(yield_by_query.items(),
                                  key=lambda kv: -kv[1][1]):
            print(f"  {hit:4d} / {n:4d}  {hit / n * 100:5.1f}%  {q}")


if __name__ == "__main__":
    main()
