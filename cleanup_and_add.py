"""
One-time cleanup of corrections_raw.json.
Removes entries with placeholder correction text ("Rettelsestekst ikke tilgjengelig").
Run before backfill to start with clean data.
"""
import json

DATA_FILE = "data/corrections_raw.json"

with open(DATA_FILE, encoding="utf-8") as f:
    corrections = json.load(f)

print(f"Loaded {len(corrections)} entries.")

before = len(corrections)
corrections = [
    e for e in corrections
    if "ikke tilgjengelig" not in e.get("correction_text_raw", "")
]
removed = before - len(corrections)
print(f"Removed {removed} placeholder entries ('Rettelsestekst ikke tilgjengelig').")

with open(DATA_FILE, "w", encoding="utf-8") as f:
    json.dump(corrections, f, ensure_ascii=False, indent=2)

print(f"Done. {len(corrections)} entries in {DATA_FILE}.")
