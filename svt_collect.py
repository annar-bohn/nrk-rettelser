"""
SVT Rättelser – sitemap-based collector.
Phase 2 of the SVT expansion plan (.claude/plans/svt.md §2, §4).

Strategy:
  Polls https://www.svt.se/latest-articles-sitemap.xml (~450 entries,
  ~48h modification-driven window, Google News format). Corrected articles
  resurface in the sitemap with fresh lastmod, so corrections are caught
  at correction time without a full crawl.

Usage:
  python3 svt_collect.py                        # run over live sitemap window
  python3 svt_collect.py --max-urls N           # cheap test run (N article fetches)
  python3 svt_collect.py --fixture URL [URL ...]  # gate check: specific URLs
"""

import argparse
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR = "data/svt"
DATA_FILE = os.path.join(DATA_DIR, "corrections_raw.json")

HEADERS = {
    "User-Agent": "SVT-Rattelser-Bot/1.0 (+https://github.com/annar-bohn/nrk-rettelser)"
}

SITEMAP_INDEX = "https://www.svt.se/sitemap.xml"
SITEMAP_NEWS = "https://www.svt.se/latest-articles-sitemap.xml"

NS_NEWS = "http://www.google.com/schemas/sitemap-news/0.9"

FETCH_SLEEP = 0.5   # seconds between fetches (polite rate)
FETCH_TIMEOUT = 12  # seconds per request

# ---------------------------------------------------------------------------
# Trigger configuration (Swedish)
# ---------------------------------------------------------------------------

# Label regex: "Rättelse:", "RÄTTELSE.", "Förtydligande:" etc.
# Anchored to start of paragraph string for per-paragraph checks.
LABEL_RE = re.compile(r"^(rättelse|förtydligande)[:.\s]", re.IGNORECASE)

# Phrase trigger — "i en tidigare version av artikeln/texten/videon/inslaget/…"
# All observed variants share this prefix.
PHRASE_TRIGGER = "i en tidigare version"

# Bare-word triggers with Unicode-aware word boundaries.
# Swedish is full of compounds: berättelse (story), berättat (told),
# upprättat, inrättat, avrättelse — these must NOT fire.
# Python's re module treats ä/å/ö as \w in Unicode mode (the default),
# so \b correctly does not fire inside these compounds.
BARE_TRIGGERS_RE = re.compile(r"\b(rättelse|rättat)\b", re.IGNORECASE)

# Candidate triggers: in a clearly-marked list so we can measure noise and
# drop them if too many false positives appear. See the dry-run report for
# the verdict on each.
CANDIDATE_TRIGGERS = [
    "det stämmer inte",
    "rätt är att",
]

# Compound words that contain trigger substrings but must NOT fire bare triggers.
# Tracked across fetched pages to demonstrate the word-boundary guard is working.
COMPOUND_RE = re.compile(r"berättelse|berättat", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------------

def has_active_trigger(text):
    """True if text contains any active trigger (label/phrase/bare-word).

    Excludes CANDIDATE_TRIGGERS — used to distinguish candidate-only hits
    from genuine trigger hits for the false-positive report.
    """
    t = text.lower()
    if PHRASE_TRIGGER in t:
        return True
    if BARE_TRIGGERS_RE.search(text):
        return True
    # For the pre-check, also catch label patterns via substring before
    # doing the anchored per-paragraph check
    if "rättelse" in t or "förtydligande" in t:
        return True
    return False


def has_trigger(text):
    """Full-page pre-check: any correction trigger present anywhere?

    Inclusive of candidates — generous to avoid missing corrections.
    The expensive per-paragraph extraction step does the real filtering.
    """
    if has_active_trigger(text):
        return True
    t = text.lower()
    for phrase in CANDIDATE_TRIGGERS:
        if phrase in t:
            return True
    return False


def has_paragraph_trigger(text):
    """True if this specific paragraph text contains a correction trigger.

    Uses label regex anchored to start of paragraph, phrase trigger,
    bare-word regex (word boundaries prevent compound matches), and
    candidate triggers.
    """
    stripped = text.strip()
    if LABEL_RE.match(stripped):
        return True
    t = text.lower()
    if PHRASE_TRIGGER in t:
        return True
    if BARE_TRIGGERS_RE.search(text):
        return True
    for phrase in CANDIDATE_TRIGGERS:
        if phrase in t:
            return True
    return False


def paragraph_active_trigger(text):
    """True if this paragraph has an active (non-candidate) trigger."""
    stripped = text.strip()
    if LABEL_RE.match(stripped):
        return True
    t = text.lower()
    if PHRASE_TRIGGER in t:
        return True
    if BARE_TRIGGERS_RE.search(text):
        return True
    return False


# SVT editorial boilerplate that appears at the bottom of many articles
# (especially older ones). Used to stop extraction in standalone case.
SVT_BOILERPLATE_RE = re.compile(
    r"svt.?s nyheter ska stå|läs mer om hur vi arbetar",
    re.IGNORECASE,
)

# Article metadata paragraphs in older SVT pages (e.g. "Uppdaterad 28 november…")
# — not correction text, filtered in standalone extraction.
SVT_METADATA_RE = re.compile(r"^(uppdaterad|publicerad)\s+\d", re.IGNORECASE)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

SKIP_PATH_PREFIXES = (
    "/nyheter/om/",    # topic/subject pages (not articles)
    "/nyheter/video/", # video pages — corrections there live in video
                       # descriptions; noted for Phase 2+ scope
)


def canonical_url(url):
    """Strip query string and fragment (live-blog permalinks carry ?inlagg=<hex>)."""
    p = urlparse(url)
    return p._replace(query="", fragment="").geturl()


def should_skip(url):
    """Return (should_skip: bool, reason: str)."""
    if not url.startswith("https://www.svt.se/"):
        return True, "not svt.se"
    path = urlparse(url).path
    if path.endswith("/"):
        return True, "trailing slash (section index)"
    for prefix in SKIP_PATH_PREFIXES:
        if path.startswith(prefix):
            return True, f"skip prefix {prefix!r}"
    return False, ""


def extract_section(url):
    """Section = URL path minus leading slash minus the final slug segment.

    Examples:
      /nyheter/inrikes/some-article      → nyheter/inrikes
      /nyheter/lokalt/vast/article-slug  → nyheter/lokalt/vast
      /kultur/some-article               → kultur
      /sport/fotboll/some-match          → sport/fotboll
    """
    parts = urlparse(url).path.strip("/").split("/")
    if len(parts) > 1:
        return "/".join(parts[:-1])
    return parts[0] if parts else ""


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def collapse_ws(text):
    """Collapse runs of whitespace to a single space."""
    return " ".join(text.split())


def extract_jsonld(soup):
    """Return the first NewsArticle JSON-LD object, or {}."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") == "NewsArticle":
                        return item
            elif isinstance(data, dict) and data.get("@type") == "NewsArticle":
                return data
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


def extract_title(soup, jsonld):
    """Extract headline: JSON-LD headline → <h1>."""
    headline = (jsonld.get("headline") or "").strip()
    if headline:
        return headline[:200]
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)[:200]
    return ""


def extract_date(soup, jsonld, sitemap_lastmod=""):
    """Extract publication date (ISO-8601 string).

    Priority: JSON-LD datePublished → <time datetime> → sitemap lastmod.
    SVT publishes well-formed JSON-LD so this rarely falls past the first step.
    Never returns empty string — schema.md requires a non-empty date for SVT.
    """
    d = (jsonld.get("datePublished") or "").strip()
    if d:
        return d
    time_el = soup.find("time", attrs={"datetime": True})
    if time_el:
        d = (time_el.get("datetime") or "").strip()
        if d:
            return d
    if sitemap_lastmod:
        return sitemap_lastmod
    # Last resort: current time (should not normally happen for SVT)
    return datetime.now(timezone.utc).isoformat()


def is_standalone_correction_article(title):
    """True if the article headline itself is "Rättelse" or "Förtydligande".

    Legacy mini-article style (e.g. the 2009 example still live). In these
    articles the body is the correction; there is no separate labeled paragraph.
    """
    return bool(re.match(r"^(rättelse|förtydligande)\s*$", title.strip(), re.IGNORECASE))


def extract_correction_blocks(soup, title=""):
    """Extract correction text from an SVT article.

    Corrections are plain <p> at the END of the article body. The label
    ("Rättelse:", "RÄTTELSE.") is usually inside <em>/<strong>. No
    dedicated CSS class; class names are build-hashed — text-pattern only.

    get_text(" ", strip=True) with whitespace collapsing avoids the NRK
    missing-spaces wart (get_text(strip=True) concatenates inline elements
    without spaces).

    Inline link text is kept naturally (get_text includes <a> text).

    Also handles the legacy standalone case: if the headline is "Rättelse",
    the whole body is the correction.

    Returns correction string, or None if nothing clean was found.
    """
    # Legacy standalone: headline = "Rättelse" → body is the correction.
    # Stop at SVT editorial boilerplate ("SVT:s nyheter ska stå för saklighet
    # och opartiskhet…") that appears at the bottom of older articles.
    if is_standalone_correction_article(title):
        paragraphs = []
        for p in soup.find_all("p"):
            text = collapse_ws(p.get_text(" ", strip=True))
            if not text or len(text) <= 5:
                continue
            if SVT_BOILERPLATE_RE.search(text):
                break  # stop before SVT editorial mission statement
            if SVT_METADATA_RE.match(text):
                continue  # skip "Uppdaterad …" / "Publicerad …" date lines
            paragraphs.append(text)
        if paragraphs:
            combined = " ".join(paragraphs)[:2000]
            return combined if combined.strip() else None

    # Standard case: scan all <p> elements for correction triggers
    blocks = []
    all_ps = soup.find_all("p")
    for el in all_ps:
        text = collapse_ws(el.get_text(" ", strip=True))
        if not text:
            continue
        if len(text) > 2000:
            continue
        if not has_paragraph_trigger(text):
            continue

        # Bare-label guard: if this <p> is just the label (≤20 chars after
        # stripping trailing ':'), merge the next sibling <p>.
        # This is the SVT day-one port of the NRK bare-label-stub lesson —
        # the validator hard-fails stubs (bare RÄTTELSE / FÖRTYDLIGANDE).
        label_stripped = text.rstrip(":").strip()
        if len(label_stripped) <= 20:
            sibling = el.find_next_sibling("p")
            if sibling:
                sib_text = collapse_ws(sibling.get_text(" ", strip=True))
                if sib_text and len(sib_text) < 2000:
                    text = text + " " + sib_text

        blocks.append(text[:2000])

    if not blocks:
        return None

    # Deduplicate: remove blocks that are substrings of a longer block
    if len(blocks) > 1:
        blocks.sort(key=len, reverse=True)
        deduped = []
        for b in blocks:
            if not any(b in existing for existing in deduped):
                deduped.append(b)
        blocks = deduped

    return " | ".join(blocks) if blocks else None


# ---------------------------------------------------------------------------
# Sitemap
# ---------------------------------------------------------------------------

def fetch_sitemap_entries():
    """Fetch and parse latest-articles-sitemap.xml.

    First verifies that sitemap.xml lists latest-articles-sitemap.xml
    (confirming the expected sitemap shape from recon). Then fetches the
    Google News sitemap and returns list of (url, lastmod) pairs.
    """
    try:
        r = requests.get(SITEMAP_INDEX, headers=HEADERS, timeout=FETCH_TIMEOUT)
        if "latest-articles-sitemap.xml" in r.text:
            print(f"Confirmed: {SITEMAP_INDEX} references latest-articles-sitemap.xml")
        else:
            print(f"WARNING: {SITEMAP_INDEX} does not reference latest-articles-sitemap.xml — fetching anyway")
    except Exception as e:
        print(f"WARNING: Could not fetch sitemap index: {e}")

    try:
        r = requests.get(SITEMAP_NEWS, headers=HEADERS, timeout=FETCH_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        print(f"ERROR: Could not fetch {SITEMAP_NEWS}: {e}")
        return []

    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        print(f"ERROR: Could not parse sitemap XML: {e}")
        return []

    ns = {
        "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
        "news": NS_NEWS,
    }

    entries = []
    for url_el in root.findall("sm:url", ns):
        loc = url_el.findtext("sm:loc", namespaces=ns)
        lastmod = url_el.findtext("sm:lastmod", namespaces=ns) or ""
        if loc:
            entries.append((loc.strip(), lastmod.strip()))

    return entries


# ---------------------------------------------------------------------------
# Article fetch and processing
# ---------------------------------------------------------------------------

def fetch_with_retry(url):
    """Fetch URL, retry once on 5xx or timeout. Returns Response or None."""
    for attempt in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=FETCH_TIMEOUT)
            if r.status_code == 200:
                return r
            if r.status_code >= 500 and attempt == 0:
                time.sleep(FETCH_SLEEP)
                continue
            print(f"  HTTP {r.status_code}: {url}")
            return None
        except requests.exceptions.Timeout:
            if attempt == 0:
                time.sleep(FETCH_SLEEP)
                continue
            print(f"  Timeout (retry exhausted): {url}")
            return None
        except requests.exceptions.RequestException as e:
            print(f"  Request error: {e}")
            return None
    return None


def process_article(url, sitemap_lastmod, corrections, existing_urls, stats, verbose=False):
    """Fetch and process one article URL. Mutates corrections, existing_urls, stats.

    verbose=True prints the extracted correction verbatim (used for gate checks).
    Returns the extracted correction string, or None.
    """
    r = fetch_with_retry(url)
    if r is None:
        return None

    # Parse bytes — never r.text. BeautifulSoup sniffs charset from <meta> tags.
    # SVT declares UTF-8 today but we parse bytes following the NRK lesson:
    # charset in Content-Type can be missing or wrong.
    soup = BeautifulSoup(r.content, "html.parser")
    page_text = soup.get_text()

    # Track compound-word occurrences (false-positive guard demonstration).
    # A page where "berättelse"/"berättat" appear but BARE_TRIGGERS_RE does NOT
    # fire proves the word-boundary guard is working correctly.
    if COMPOUND_RE.search(page_text):
        if not BARE_TRIGGERS_RE.search(page_text):
            stats["compound_guard_worked"] += 1
        else:
            stats["compound_with_real_trigger"] += 1

    # Cheap full-page pre-check (same pattern as scraper.py for NRK)
    if not has_trigger(page_text):
        return None

    # Distinguish candidate-only hits from active-trigger hits
    active = has_active_trigger(page_text)
    if not active:
        stats["candidate_only_precheck"] += 1

    stats["trigger_hits"] += 1

    # Extract metadata
    jsonld = extract_jsonld(soup)
    title = extract_title(soup, jsonld)
    pub_date = extract_date(soup, jsonld, sitemap_lastmod)
    modified_date = (jsonld.get("dateModified") or "").strip()
    section = extract_section(url)

    # Extract correction text
    correction = extract_correction_blocks(soup, title)
    if correction is None:
        if not active:
            stats["candidate_only_no_extraction"] += 1
        return None

    if not active:
        stats["candidate_only_extracted"] += 1

    stats["extracted"] += 1

    if verbose:
        print(f"\n{'=' * 72}")
        print(f"URL:       {url}")
        print(f"Title:     {title}")
        print(f"Date:      {pub_date}")
        print(f"Section:   {section}")
        print(f"Correction text (verbatim):")
        print(f"  {correction}")

    if url not in existing_urls:
        entry = {
            "id": int(time.time() * 1000),
            "url": url,
            "date": pub_date,
            "title": title or url,
            "correction": correction,
            "correction_text_raw": correction,
            "qa_status": "pending",
            "auto": True,
            "source": "sitemap",
            "outlet": "svt",
            "section": section,
            "publication_date": pub_date,
            "modified_date": modified_date,
        }
        corrections.append(entry)
        existing_urls.add(url)
        if not verbose:
            print(f"  -> Rättelse hittad: {title[:70]}")

    return correction


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="SVT Rättelser collector — sitemap-based, Phase 2."
    )
    parser.add_argument(
        "--max-urls", type=int, default=0,
        help="Limit article fetches (0 = no limit). E.g. --max-urls 20 for cheap test runs.",
    )
    parser.add_argument(
        "--fixture", nargs="+", metavar="URL",
        help="Skip sitemap; fetch only these URLs. Used for the Phase 2 gate check.",
    )
    args = parser.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)

    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            corrections = json.load(f)
    else:
        corrections = []

    existing_urls = {c["url"] for c in corrections}
    initial_count = len(corrections)

    stats = {
        "listed": 0,
        "skipped_filter": 0,
        "already_known": 0,
        "fetched": 0,
        "trigger_hits": 0,
        "extracted": 0,
        "compound_guard_worked": 0,
        "compound_with_real_trigger": 0,
        "candidate_only_precheck": 0,
        "candidate_only_no_extraction": 0,
        "candidate_only_extracted": 0,
    }

    if args.fixture:
        print("=== SVT Rättelser — fixture gate check ===\n")
        print(f"Fetching {len(args.fixture)} specific URLs:\n")
        for url in args.fixture:
            canon = canonical_url(url)
            print(f"Checking: {canon}")
            stats["fetched"] += 1
            process_article(canon, "", corrections, existing_urls, stats, verbose=True)
            time.sleep(FETCH_SLEEP)

    else:
        print("=== SVT Rättelser — sitemap scan ===\n")
        sitemap_entries = fetch_sitemap_entries()
        stats["listed"] = len(sitemap_entries)
        print(f"\nSitemap: {stats['listed']} entries listed")

        to_fetch = []
        for raw_url, lastmod in sitemap_entries:
            canon = canonical_url(raw_url)
            skip, reason = should_skip(canon)
            if skip:
                stats["skipped_filter"] += 1
                continue
            if canon in existing_urls:
                stats["already_known"] += 1
                continue
            to_fetch.append((canon, lastmod))

        print(
            f"After filter: {stats['skipped_filter']} skipped, "
            f"{stats['already_known']} already known, "
            f"{len(to_fetch)} to fetch"
        )

        if args.max_urls and len(to_fetch) > args.max_urls:
            print(f"--max-urls {args.max_urls}: capping at {args.max_urls} of {len(to_fetch)} articles")
            to_fetch = to_fetch[: args.max_urls]

        print(f"\nFetching {len(to_fetch)} articles (polite rate: {FETCH_SLEEP}s sleep)\n")

        for canon, lastmod in to_fetch:
            stats["fetched"] += 1
            time.sleep(FETCH_SLEEP)
            process_article(canon, lastmod, corrections, existing_urls, stats)

    # ---------------------------------------------------------------------------
    # Candidate trigger verdict
    # ---------------------------------------------------------------------------
    print("\n--- CANDIDATE_TRIGGERS verdict ---")
    print("Candidates: 'det stämmer inte', 'rätt är att'")
    print(f"  Pages triggering ONLY via candidates (not label/phrase/bare-word): {stats['candidate_only_precheck']}")
    print(f"    Of those — corrections extracted: {stats['candidate_only_extracted']}")
    print(f"    Of those — no extraction (false-positive pre-check):              {stats['candidate_only_no_extraction']}")

    cp = stats["candidate_only_precheck"]
    cx = stats["candidate_only_extracted"]
    cn = stats["candidate_only_no_extraction"]

    if cp == 0:
        print("  VERDICT: No candidate-only hits in this window — insufficient data.")
        print("  Keeping in CANDIDATE_TRIGGERS (not promoted to active triggers).")
        print("  Recommend monitoring across several windows before deciding.")
    elif cn > cx:
        print(f"  VERDICT: More false-positive hits ({cn}) than corrections ({cx}).")
        print("  Recommendation: drop 'det stämmer inte' and 'rätt är att' from active triggers.")
        print("  They remain in CANDIDATE_TRIGGERS for later re-evaluation if SVT patterns change.")
    else:
        print(f"  VERDICT: Candidates extracted {cx} corrections with only {cn} false-positive pre-checks.")
        print("  Keeping in CANDIDATE_TRIGGERS; promote to active triggers if yield stays consistent.")

    # ---------------------------------------------------------------------------
    # Bare-word guard report
    # ---------------------------------------------------------------------------
    print("\n--- Bare-word guard (berättelse / berättat) ---")
    print(f"  Fetched pages with compound words present, BARE_TRIGGERS_RE NOT firing: {stats['compound_guard_worked']}")
    print(f"  Fetched pages with compound words AND a real bare trigger (co-occurrence): {stats['compound_with_real_trigger']}")
    if stats["compound_guard_worked"] > 0:
        print("  Guard is working: compound words did not cause false-positive extractions.")
    else:
        print("  No compound-word pages fetched in this run (or none without a real trigger).")

    # ---------------------------------------------------------------------------
    # Run summary
    # ---------------------------------------------------------------------------
    new_count = len(corrections) - initial_count
    print("\n=== Run summary ===")
    if not args.fixture:
        print(f"  URLs listed in sitemap:      {stats['listed']}")
        print(f"  Skipped by filter:           {stats['skipped_filter']}")
        print(f"  Already in dataset:          {stats['already_known']}")
        print(f"  Fetched:                     {stats['fetched']}")
    print(f"  Trigger pre-check hits:      {stats['trigger_hits']}")
    print(f"  Corrections extracted:        {stats['extracted']}")
    print(f"  New entries added:            {new_count}")
    print(f"  Total dataset size:           {len(corrections)}")

    # ---------------------------------------------------------------------------
    # Save
    # ---------------------------------------------------------------------------
    if new_count > 0 or (not os.path.exists(DATA_FILE) and len(corrections) > 0):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(corrections, f, ensure_ascii=False, indent=2)
        print(f"\nSaved {len(corrections)} entries to {DATA_FILE}")
    else:
        print(f"\nNo new entries — {DATA_FILE} unchanged.")


if __name__ == "__main__":
    main()
