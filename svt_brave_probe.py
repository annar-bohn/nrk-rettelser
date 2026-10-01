"""
SVT Rättelser – Brave Search recall probe (exploratory, read-only).

svt_audit.py's first Brave pilot surfaced only ~1 in 4 of the 2026 corrections
the sitemap collector already holds. This script measures which query
variations lift that: it runs groups of variants against 2026, and scores each
by how many already-collected 2026 entries ("known") appear in its results.

Writes nothing. Results go to stdout; winners are ported to
svt_audit.BRAVE_QUERIES by hand.

Usage:  BRAVE_API_KEY=… python3 svt_brave_probe.py [--groups baseline,phrasing]
"""

import argparse
import os
import sys
import time

import requests

import svt_audit as audit
import svt_collect as sc

YEAR = "2026-01-01to2026-12-31"
LOCALE = {"country": "SE", "search_lang": "sv"}
BASE = audit.BRAVE_QUERIES

PHRASES = [
    '"tidigare version av artikeln"', '"tidigare version av videon"',
    '"tidigare version av klippet"', '"tidigare version av texten"',
    '"tidigare version stod"', '"tidigare version skrev vi"',
    '"tidigare version"', '"det stämmer inte" "tidigare version"',
    '"rätt är att"', '"korrekt är att"', '"har förtydligats"',
    '"artikeln har uppdaterats"', '"tidigare uppgav vi"', 'rättelse tidigare version',
    '"Rättelse"', 'förtydligande',
]

SECTIONS = ["nyheter/inrikes", "nyheter/utrikes", "nyheter/lokalt", "nyheter/lokalt/vast",
            "nyheter/lokalt/varmland", "nyheter/lokalt/skane", "sport", "kultur"]

requests_made = 0


def search(query, freshness=None, locale=True, max_pages=10, count=20):
    """Page one query to exhaustion. Returns canonical URLs (may be empty)."""
    global requests_made
    urls = []
    for offset in range(max_pages):
        params = {"q": query, "count": count, "offset": offset,
                  "result_filter": "web", "text_decorations": "false"}
        if freshness:
            params["freshness"] = freshness
        if locale:
            params.update(LOCALE)
        data = None
        for attempt in range(3):
            r = requests.get(audit.BRAVE_URL, params=params, timeout=30, headers={
                "Accept": "application/json",
                "X-Subscription-Token": os.environ["BRAVE_API_KEY"]})
            requests_made += 1
            time.sleep(audit.BRAVE_SLEEP)
            if r.status_code == 200:
                data = r.json()
                break
            print(f"    HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code not in (429, 500, 502, 503, 504):
                break
            time.sleep(5)
        if data is None:
            break
        results = (data.get("web") or {}).get("results") or []
        urls += [sc.canonical_url(x["url"]).replace("http://", "https://", 1)
                 for x in results if x.get("url")]
        if not results or not (data.get("query") or {}).get("more_results_available"):
            break
    return urls


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--groups", default="")
    args = parser.parse_args()
    if not os.environ.get("BRAVE_API_KEY", "").strip():
        sys.exit("BRAVE_API_KEY is not set.")

    raw = [c for c in audit.load_dataset() if (c.get("date") or "").startswith("2026")]
    known = {c["url"] for c in raw}
    months = sorted({c["date"][:7] for c in raw})
    print(f"Known 2026 entries: {len(known)}\n")

    month_ranges = [audit.month_slices(2026)[int(m[5:]) - 1] for m in months]
    groups = {
        "baseline": [(q, q, YEAR, True) for q in BASE],
        "no_locale": [(q, q, YEAR, False) for q in BASE],
        "no_freshness": [(q, q, None, True) for q in BASE],
        "monthly": [(f"{q} [{fr[:7]}]", q, fr, True)
                    for q in (BASE[0], BASE[2], BASE[4]) for fr in month_ranges],
        "phrasing": [(p, f"site:svt.se {p}", YEAR, True) for p in PHRASES],
        "operators": [(q, q, YEAR, True) for q in (
            'site:svt.se inbody:"rättelse"', 'site:svt.se inbody:"tidigare version"',
            'site:svt.se intitle:rättelse', 'svt.se rättelse "tidigare version"',
            'svt "Rättelse:" "i en tidigare version"')],
        "sections": [(f"{s} / {w}", f"site:svt.se/{s} {w}", YEAR, True)
                     for s in SECTIONS for w in ("rättelse", '"tidigare version"')],
    }
    wanted = [g for g in args.groups.split(",") if g] or list(groups) + ["indexed"]

    overall, all_new = set(), set()
    for name in wanted:
        if name == "indexed":
            continue
        print(f"== {name} ==")
        g_hits, g_new = set(), set()
        for label, query, freshness, locale in groups[name]:
            urls = set(search(query, freshness, locale))
            hits = urls & known
            new = {u for u in urls - known if not sc.should_skip(u)[0]}
            g_hits |= hits
            g_new |= new
            print(f"  {len(urls):4d} results  {len(hits):2d} known  {len(new):4d} other   {label}")
        print(f"  -> group union: {len(g_hits)}/{len(known)} known, {len(g_new)} other URLs\n")
        overall |= g_hits
        all_new |= g_new

    print(f"OVERALL union: {len(overall)}/{len(known)} known, "
          f"{len(all_new)} other candidate URLs")
    by_month = {m: [0, 0] for m in months}
    for c in raw:
        by_month[c["date"][:7]][1] += 1
        by_month[c["date"][:7]][0] += c["url"] in overall
    print("  by month: " + "  ".join(f"{m[5:]}: {a}/{b}" for m, (a, b) in by_month.items()))

    if "indexed" in wanted:
        # Is each known article in Brave's index at all? Search its own title.
        # If not, no trigger query can ever surface it — that is the ceiling.
        print("\n== indexed (title lookup, no date filter) ==")
        indexed = set()
        for c in raw:
            title = (c.get("title") or "").replace('"', " ").strip()
            urls = search(f'site:svt.se "{title}"', max_pages=1)
            if c["url"] in urls:
                indexed.add(c["url"])
            print(f"  {'IDX' if c['url'] in urls else '---'} "
                  f"{'found' if c['url'] in overall else 'MISS '} {c['date'][:10]} {title[:70]}")
        print(f"  -> {len(indexed)}/{len(known)} in Brave's index; "
              f"{len(indexed & overall)} of those surfaced by a trigger query; "
              f"{len(overall - indexed)} surfaced but not matched by title lookup")

    print(f"\nBrave requests made: {requests_made}")


if __name__ == "__main__":
    main()
