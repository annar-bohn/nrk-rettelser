"""
One-time migration: apply trim_to_correction() to all existing entries
in corrections_raw.json and corrections.json.

Trims correction_text_raw and correction fields to remove article intro,
image captions, and AI-summary boilerplate that was captured before the
actual correction text.

Safe to run multiple times — already-clean entries are unchanged.
"""

import json
import re

TRIGGERS = [
    "i en tidligere versjon", "i en eldre versjon", "i en tidligere publisert versjon",
    "nrk retter", "nrk har rettet", "nrk korrigerer", "nrk beklager",
    "rettelse:", "rettelse", "retting:", "retting", "korrigering:",
    "presisering:", "endringen er gjort", "endringane er gjort",
    "endringane vart gjort", "det er gjort endringar", "vi har rettet",
    "artikkelen er oppdatert", "artikkelen er endra", "tidligere skrev vi",
    "etter publisering",
]
BARE_TRIGGERS_RE = re.compile(r'\b(rettelse|retting)\b', re.IGNORECASE)


def trim_to_correction(text):
    """Trim text to start from the sentence containing the first trigger phrase.

    Prefers strong multi-word triggers over bare "rettelse"/"retting" which
    often appear in non-correction contexts.
    """
    t_lower = text.lower()

    # Check if text already starts with a trigger-like word (possibly
    # joined without space, e.g. "RETTINGDet", "Rettelser:", "RETTELSEN:")
    if re.match(r'(?i)(rettels\w*|retting\w*|presisering\w*|korrigering\w*)', t_lower):
        return text  # already starts at correction, no trimming needed

    # First pass: look for strong triggers only (multi-word phrases)
    earliest_pos = len(text)
    for phrase in TRIGGERS:
        if phrase in ("rettelse", "retting"):
            continue
        pos = t_lower.find(phrase)
        if pos != -1 and pos < earliest_pos:
            earliest_pos = pos

    # Second pass: fall back to bare words only if no strong trigger found
    if earliest_pos == len(text):
        m = BARE_TRIGGERS_RE.search(t_lower)
        if m:
            earliest_pos = m.start()

    if earliest_pos == len(text):
        return text

    # Walk back to sentence boundary
    chunk = text[:earliest_pos]

    # Try ". " first (normal sentence boundary)
    boundary = chunk.rfind(". ")
    if boundary != -1:
        trimmed = text[boundary + 2:].strip()
        return trimmed if trimmed else text

    # Try "." right before the trigger (HTML joins without spaces)
    if chunk.endswith("."):
        trimmed = text[earliest_pos:].strip()
        return trimmed if trimmed else text

    # Try "." followed by uppercase earlier in the chunk
    m = re.search(r'\.[A-ZÆØÅ][^.]*$', chunk)
    if m:
        trimmed = text[m.start() + 1:].strip()
        return trimmed if trimmed else text

    # Try newline
    last_nl = chunk.rfind("\n")
    if last_nl != -1:
        trimmed = text[last_nl + 1:].strip()
        return trimmed if trimmed else text

    return text.strip()


def migrate_file(path):
    with open(path, encoding="utf-8") as f:
        entries = json.load(f)

    changed = 0
    for entry in entries:
        for field in ("correction_text_raw", "correction"):
            if field in entry and entry[field]:
                trimmed = trim_to_correction(entry[field])
                if trimmed != entry[field]:
                    entry[field] = trimmed
                    changed += 1

    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)

    return len(entries), changed


if __name__ == "__main__":
    for path in ("data/corrections_raw.json", "data/corrections.json"):
        total, changed = migrate_file(path)
        print(f"{path}: {total} entries, {changed} fields trimmed")
