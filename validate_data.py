"""
validate_data.py — read-only contract checker for nrk-rettelser data files.

Checks the raw (append-only working set) and frontend (regenerated,
QA-filtered) JSON files for each outlet against the shared record contract
documented in schema.md. Never writes anything.

Severity model — "pin current, fail new":
  - FAIL: a contract violation. Any FAIL makes the run exit 1.
  - WARN: a violation that is already present in committed data and has been
    explicitly grandfathered (by URL, with a reason) in GRANDFATHERED below.
    An occurrence of the same check on a URL NOT listed there is a FAIL, not
    a WARN — grandfathering never grows silently.

Usage:
  python3 validate_data.py [--outlet nrk|svt|all]
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

# ---------------------------------------------------------------------------
# Contract constants — single source of truth; schema.md must match these.
# ---------------------------------------------------------------------------

NEWS_CATEGORIES = {
    "sports", "culture", "politics", "economy", "science", "health",
    "technology", "local", "world", "crime", "weather", "entertainment",
    "other",
}

CORRECTION_TYPES = {
    "factual_error", "wrong_name", "wrong_number", "wrong_image", "wrong_date",
    "wrong_location", "mistranslation", "misleading_title", "missing_context",
    "source_error", "retracted_claim", "spelling_grammar", "attribution_error",
    "other",
}

QA_STATUSES_RAW = {"genuine_correction", "uncertain", "not_a_correction", "pending"}
INCLUDE_STATUSES = {"genuine_correction", "uncertain", "pending"}

STUB_LABELS = {
    "nrk": {"RETTING", "RETTELSE", "PRESISERING"},
    "svt": {"RÄTTELSE", "FÖRTYDLIGANDE"},
}

URL_PREFIXES = {
    "nrk": "https://www.nrk.no/",
    "svt": "https://www.svt.se/",
}

# Exact frontend key set per outlet. SVT swaps NRK's `nrk_section` for a
# generic `section` plus `outlet` (constant "svt") — see schema.md.
_FRONTEND_COMMON_KEYS = {
    "id", "url", "date", "title", "headline", "correction",
    "correction_text_raw", "correction_description", "correction_text_extract",
    "correction_date", "qa_status", "publication_date", "modified_date",
    "news_category", "correction_type", "journalist", "responsible_editor",
    "time_to_correct_hours", "auto", "source",
}
FRONTEND_KEYS = {
    "nrk": _FRONTEND_COMMON_KEYS | {"nrk_section"},
    "svt": _FRONTEND_COMMON_KEYS | {"section", "outlet"},
}
FRONTEND_STR_FIELDS = {
    outlet: keys - {"id", "auto", "time_to_correct_hours", "correction_date"}
    for outlet, keys in FRONTEND_KEYS.items()
}

# Fields that only belong in the raw working set (full article text) — never
# allowed in a frontend file. Also excluded from FRONTEND_KEYS above, so
# exact_key_set already catches these; this is a named guard for clarity.
FORBIDDEN_FRONTEND_FIELDS = {"article_body", "intro_text"}

RAW_REQUIRED_FIELDS = {
    "id", "url", "date", "title", "correction", "correction_text_raw",
    "qa_status", "auto", "source",
}

FILES = {
    "nrk": {
        "raw": "data/corrections_raw.json",
        "frontend": "data/corrections.json",
    },
    "svt": {
        "raw": "data/svt/corrections_raw.json",
        "frontend": "data/svt/corrections.json",
    },
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DETAIL_TRUNCATE = 160

# ---------------------------------------------------------------------------
# GRANDFATHERED — pin current, fail new.
#
# Keyed by outlet -> check name -> URL -> short reason. A violation of
# `check` on `url` is downgraded from FAIL to WARN only if it is listed here.
# Everything else is a FAIL. Do not add entries to make a violation of a NEW
# class of data disappear — only to acknowledge violations that already
# exist in committed data, so the gate can go green without masking new bugs.
#
# Measured against the live repo 2026-08-20 (raw=1941, frontend=1533).
# ---------------------------------------------------------------------------

GRANDFATHERED = {
    "nrk": {
        # 3 of these 5 are the "exactly 3 known warts" documented in
        # CLAUDE.md (that count was measured against the frontend file
        # only). The other 2 only ever existed in the raw file, on entries
        # QA rejected as not_a_correction, so they never surfaced before —
        # newly discovered while building this validator, not previously
        # tracked anywhere.
        "date_format": {
            "https://www.nrk.no/nyheter/krever-ett-ars-fengsel-for-isak-dreyer-1.17824825":
                "RFC-1123 string 'Wed, 25 Mar 2026 13:29:20 GMT', not ISO-8601 "
                "(CLAUDE.md known wart)",
            "https://www.nrk.no/nyttig/xl/djesa-drommer-om-a-bli-gjeldfri-1.17668616":
                "RFC-1123 string 'Wed, 21 Jan 2026 12:19:57 GMT', not ISO-8601 "
                "(CLAUDE.md known wart)",
            "https://www.nrk.no/dokumentar/xl/psyk_-del-4_-lytt-til-de-andre-1.17171747":
                "empty date string (CLAUDE.md known wart)",
            "https://www.nrk.no/nyttig/xl/karen-guiden_-en-guide-til-forbrukerrettigheter-1.17758500":
                "RFC-1123 string 'Sun, 22 Mar 2026 06:43:51 GMT'; raw-only "
                "(qa_status=not_a_correction, excluded from frontend) — newly "
                "discovered 2026-08-20, not previously documented",
            "https://www.nrk.no/kultur/_-stor-sjanse-for-at-dette-kan-vera-framtida-var-1.17687180":
                "empty date string; raw-only (qa_status=not_a_correction, "
                "excluded from frontend) — newly discovered 2026-08-20, not "
                "previously documented",
        },
        # Neither URL is in NEWS_CATEGORIES' 13-value enum. Both are
        # genuine_correction and appear in the frontend file too. Newly
        # discovered 2026-08-20 — likely a leftover from before the enum was
        # tightened; needs a maintainer decision (fold into an existing
        # category, e.g. "local"/"world", or add "norge" to the enum).
        "enum_news_category": {
            "https://www.nrk.no/norge/feil-i-nrk-sak-om-cruiseskipet-1.14491982":
                "news_category='norge', not in the 13-value enum — newly "
                "discovered 2026-08-20",
            "https://www.nrk.no/norge/vedtak-om-martha-louise-og-durek-verretts-gin-omgjores-_-brot-ikke-loven-likevel-1.17320557":
                "news_category='norge', not in the 13-value enum — newly "
                "discovered 2026-08-20",
        },
        # Raw entry is missing the 'source' field entirely (same entry as
        # the first date_format pin above). Newly discovered 2026-08-20 —
        # every other one of the 1941 raw entries has 'source'.
        "raw_required_fields": {
            "https://www.nrk.no/nyheter/krever-ett-ars-fengsel-for-isak-dreyer-1.17824825":
                "raw entry missing required field 'source' — newly discovered "
                "2026-08-20",
        },
    },
    "svt": {},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def normalize_dt(s):
    if s.endswith("Z"):
        return s[:-1] + "+00:00"
    return s


def is_iso_date(s):
    if not s:
        return False
    try:
        datetime.fromisoformat(normalize_dt(s))
        return True
    except (ValueError, TypeError):
        return False


def is_stub(text, outlet):
    stripped = text.strip().rstrip(":").strip().upper()
    return stripped in STUB_LABELS.get(outlet, set())


def has_mojibake(text):
    return "Ã" in text or "â" in text


def truncate(value):
    s = repr(value)
    if len(s) > DETAIL_TRUNCATE:
        s = s[:DETAIL_TRUNCATE] + "...'"
    return s


def record(findings, outlet, file_kind, check, url, detail):
    """Append a finding, downgrading FAIL->WARN if (outlet, check, url) is pinned."""
    pin_reason = GRANDFATHERED.get(outlet, {}).get(check, {}).get(url)
    findings.append({
        "outlet": outlet,
        "file": file_kind,
        "check": check,
        "url": url,
        "detail": detail,
        "severity": "WARN" if pin_reason else "FAIL",
        "reason": pin_reason,
    })


# ---------------------------------------------------------------------------
# Frontend checks (strict projection contract)
# ---------------------------------------------------------------------------

def validate_frontend(outlet, entries, findings):
    expected_keys = FRONTEND_KEYS[outlet]
    str_fields = FRONTEND_STR_FIELDS[outlet]
    prefix = URL_PREFIXES[outlet]
    seen_urls = set()

    for e in entries:
        url = e.get("url") or "<no-url>"

        keys = set(e.keys())
        missing = expected_keys - keys
        extra = keys - expected_keys
        if missing or extra:
            parts = []
            if missing:
                parts.append(f"missing={sorted(missing)}")
            if extra:
                parts.append(f"extra={sorted(extra)}")
            record(findings, outlet, "frontend", "exact_key_set", url, "; ".join(parts))

        for f in FORBIDDEN_FRONTEND_FIELDS:
            if f in e:
                record(findings, outlet, "frontend", "forbidden_field", url,
                       f"'{f}' present in frontend file (size guard)")

        # --- types ---
        idv = e.get("id")
        if not isinstance(idv, int) or isinstance(idv, bool):
            record(findings, outlet, "frontend", "field_types", url, f"id={truncate(idv)} not int")

        autov = e.get("auto")
        if not isinstance(autov, bool):
            record(findings, outlet, "frontend", "field_types", url, f"auto={truncate(autov)} not bool")

        ttc = e.get("time_to_correct_hours")
        if ttc is not None and (isinstance(ttc, bool) or not isinstance(ttc, (int, float))):
            record(findings, outlet, "frontend", "field_types", url,
                   f"time_to_correct_hours={truncate(ttc)} not int/float/null")

        cdv = e.get("correction_date")
        if cdv is not None and not isinstance(cdv, str):
            record(findings, outlet, "frontend", "field_types", url,
                   f"correction_date={truncate(cdv)} not str/null")

        for f in str_fields:
            if f in e and not isinstance(e[f], str):
                record(findings, outlet, "frontend", "field_types", url,
                       f"{f}={truncate(e[f])} not str")

        # --- qa_status ---
        status = e.get("qa_status")
        if status not in INCLUDE_STATUSES:
            record(findings, outlet, "frontend", "qa_status_enum", url,
                   f"qa_status={truncate(status)} not in INCLUDE_STATUSES")
        pending = status == "pending"

        # --- enums (empty string allowed only for pending entries) ---
        nc = e.get("news_category", "")
        if not (pending and nc == "") and nc not in NEWS_CATEGORIES:
            record(findings, outlet, "frontend", "enum_news_category", url, f"news_category={truncate(nc)}")

        ct = e.get("correction_type", "")
        if not (pending and ct == "") and ct not in CORRECTION_TYPES:
            record(findings, outlet, "frontend", "enum_correction_type", url, f"correction_type={truncate(ct)}")

        # --- correction text ---
        corr = e.get("correction") or ""
        if not corr.strip():
            record(findings, outlet, "frontend", "correction_empty", url, "correction is empty")
        elif is_stub(corr, outlet):
            record(findings, outlet, "frontend", "correction_stub", url, f"correction={truncate(corr)}")

        # --- mojibake ---
        for f in ("correction", "title", "correction_description"):
            v = e.get(f) or ""
            if has_mojibake(v):
                record(findings, outlet, "frontend", "mojibake", url, f"{f}={truncate(v[:80])}")

        # --- url prefix + uniqueness ---
        u = e.get("url", "")
        if not u.startswith(prefix):
            record(findings, outlet, "frontend", "url_prefix", url, f"url={truncate(u)} missing prefix {prefix!r}")
        if u in seen_urls:
            record(findings, outlet, "frontend", "url_uniqueness", url, "duplicate url in file")
        seen_urls.add(u)

        # --- dates ---
        if not is_iso_date(e.get("date", "")):
            record(findings, outlet, "frontend", "date_format", url, f"date={truncate(e.get('date'))}")

        for f in ("publication_date", "modified_date"):
            v = e.get(f, "")
            if v != "" and not is_iso_date(v):
                record(findings, outlet, "frontend", "pub_mod_date_format", url, f"{f}={truncate(v)}")

        cd = e.get("correction_date")
        if cd is not None and not (isinstance(cd, str) and DATE_RE.match(cd)):
            record(findings, outlet, "frontend", "correction_date_format", url, f"correction_date={truncate(cd)}")

        # --- time_to_correct_hours range ---
        if ttc is not None and isinstance(ttc, (int, float)) and not isinstance(ttc, bool):
            if not (0 <= ttc <= 43800):
                record(findings, outlet, "frontend", "time_to_correct_range", url,
                       f"time_to_correct_hours={truncate(ttc)}")


# ---------------------------------------------------------------------------
# Raw checks (looser working-set contract)
# ---------------------------------------------------------------------------

def validate_raw(outlet, entries, findings):
    prefix = URL_PREFIXES[outlet]
    seen_urls = set()

    for e in entries:
        url = e.get("url") or "<no-url>"

        missing = sorted(f for f in RAW_REQUIRED_FIELDS if f not in e)
        if missing:
            record(findings, outlet, "raw", "raw_required_fields", url, f"missing={missing}")

        status = e.get("qa_status")
        if status not in QA_STATUSES_RAW:
            record(findings, outlet, "raw", "qa_status_enum", url, f"qa_status={truncate(status)}")
        pending = status == "pending"

        if "correction" in e:
            corr = e.get("correction") or ""
            if not corr.strip():
                record(findings, outlet, "raw", "correction_empty", url, "correction is empty")
            elif is_stub(corr, outlet):
                record(findings, outlet, "raw", "correction_stub", url, f"correction={truncate(corr)}")

        for f in ("correction", "title"):
            v = e.get(f) or ""
            if has_mojibake(v):
                record(findings, outlet, "raw", "mojibake", url, f"{f}={truncate(v[:80])}")

        if "url" in e:
            u = e["url"]
            if not u.startswith(prefix):
                record(findings, outlet, "raw", "url_prefix", url, f"url={truncate(u)} missing prefix {prefix!r}")
            if u in seen_urls:
                record(findings, outlet, "raw", "url_uniqueness", url, "duplicate url in file")
            seen_urls.add(u)

        if "date" in e and not is_iso_date(e.get("date", "")):
            record(findings, outlet, "raw", "date_format", url, f"date={truncate(e.get('date'))}")

        # Enum fields are enrichment output — not required core fields, so
        # only validated when present. Pending (un-enriched) entries may
        # carry them empty or leave them out entirely.
        if "news_category" in e:
            nc = e["news_category"]
            if not (pending and nc == "") and nc not in NEWS_CATEGORIES:
                record(findings, outlet, "raw", "enum_news_category", url, f"news_category={truncate(nc)}")

        if "correction_type" in e:
            ct = e["correction_type"]
            if not (pending and ct == "") and ct not in CORRECTION_TYPES:
                record(findings, outlet, "raw", "enum_correction_type", url, f"correction_type={truncate(ct)}")

        if "correction_date" in e:
            cd = e["correction_date"]
            if cd is not None and not (isinstance(cd, str) and DATE_RE.match(cd)):
                record(findings, outlet, "raw", "correction_date_format", url, f"correction_date={truncate(cd)}")


# ---------------------------------------------------------------------------
# Cross-file consistency
# ---------------------------------------------------------------------------

def validate_cross_file(outlet, raw_entries, frontend_entries, findings):
    raw_include_urls = {e.get("url") for e in raw_entries if e.get("qa_status") in INCLUDE_STATUSES}
    frontend_urls = {e.get("url") for e in frontend_entries}

    for u in sorted(raw_include_urls - frontend_urls):
        record(findings, outlet, "cross_file", "cross_file_consistency", u,
               "in raw with an INCLUDE_STATUSES qa_status but missing from frontend")
    for u in sorted(frontend_urls - raw_include_urls):
        record(findings, outlet, "cross_file", "cross_file_consistency", u,
               "in frontend but not in raw with an INCLUDE_STATUSES qa_status")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def validate_outlet(outlet):
    findings = []
    summary = {}

    raw_path = FILES[outlet]["raw"]
    fe_path = FILES[outlet]["frontend"]

    raw_entries = None
    if os.path.exists(raw_path):
        raw_entries = load_json(raw_path)
        validate_raw(outlet, raw_entries, findings)
        summary["raw"] = len(raw_entries)
    else:
        print(f"[{outlet}] {raw_path} does not exist — skipping (not collected yet)")

    fe_entries = None
    if os.path.exists(fe_path):
        fe_entries = load_json(fe_path)
        validate_frontend(outlet, fe_entries, findings)
        summary["frontend"] = len(fe_entries)
    else:
        print(f"[{outlet}] {fe_path} does not exist — skipping (not collected yet)")

    if raw_entries is not None and fe_entries is not None:
        validate_cross_file(outlet, raw_entries, fe_entries, findings)

    return findings, summary


def print_report(all_findings, all_summaries, outlets):
    print()
    print("=" * 78)
    print("Findings")
    print("=" * 78)

    if not all_findings:
        print("(none)")

    def sort_key(f):
        return (f["outlet"], f["file"], f["check"], f["severity"], f["url"])

    for f in sorted(all_findings, key=sort_key):
        tag = f"[{f['outlet']}/{f['file']}] {f['check']}"
        line = f"{f['severity']:4s} {tag:38s} {f['url']}  {f['detail']}"
        if f["reason"]:
            line += f"  (grandfathered: {f['reason']})"
        print(line)

    print()
    print("=" * 78)
    print("Summary")
    print("=" * 78)

    total_fails = 0
    total_warns = 0
    for outlet in outlets:
        summary = all_summaries.get(outlet, {})
        outlet_findings = [f for f in all_findings if f["outlet"] == outlet]
        if not summary:
            print(f"{outlet:5s} (skipped — no files present)")
            continue
        for file_kind, count in summary.items():
            file_findings = [f for f in outlet_findings if f["file"] == file_kind]
            fails = sum(1 for f in file_findings if f["severity"] == "FAIL")
            warns = sum(1 for f in file_findings if f["severity"] == "WARN")
            total_fails += fails
            total_warns += warns
            print(f"{outlet:5s} {file_kind:10s} entries={count:6d}  fails={fails:3d}  warns={warns:3d}")
        cross_findings = [f for f in outlet_findings if f["file"] == "cross_file"]
        if cross_findings or ("raw" in summary and "frontend" in summary):
            fails = sum(1 for f in cross_findings if f["severity"] == "FAIL")
            warns = sum(1 for f in cross_findings if f["severity"] == "WARN")
            total_fails += fails
            total_warns += warns
            print(f"{outlet:5s} {'cross_file':10s} {'':14s}  fails={fails:3d}  warns={warns:3d}")

    print()
    print(f"TOTAL fails={total_fails} warns={total_warns}")
    return total_fails


def main():
    parser = argparse.ArgumentParser(description="Validate nrk-rettelser data files against the shared record contract.")
    parser.add_argument("--outlet", choices=["nrk", "svt", "all"], default="all")
    args = parser.parse_args()

    outlets = ["nrk", "svt"] if args.outlet == "all" else [args.outlet]

    print("=" * 78)
    print("nrk-rettelser data contract validator")
    print("=" * 78)

    all_findings = []
    all_summaries = {}
    for outlet in outlets:
        findings, summary = validate_outlet(outlet)
        all_findings.extend(findings)
        if summary:
            all_summaries[outlet] = summary

    total_fails = print_report(all_findings, all_summaries, outlets)

    exit_code = 1 if total_fails > 0 else 0
    print(f"Exit code: {exit_code}")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
