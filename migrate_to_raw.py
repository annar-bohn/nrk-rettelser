"""
One-time migration: copies data/corrections.json → data/corrections_raw.json
with qa_status: "pending" added to each entry (only if not already set).
Run once before deploying the new workflow.
"""
import json, os

SRC  = "data/corrections.json"
DEST = "data/corrections_raw.json"

with open(SRC, "r", encoding="utf-8") as f:
    entries = json.load(f)

for e in entries:
    # Normalise old field names
    if "correction_text_raw" not in e:
        e["correction_text_raw"] = e.get("correction", "")
    if "headline" not in e:
        e["headline"] = e.get("title", "")
    # Set pending only if not already processed
    if "qa_status" not in e:
        e["qa_status"] = "pending"

with open(DEST, "w", encoding="utf-8") as f:
    json.dump(entries, f, ensure_ascii=False, indent=2)

print(f"Written {len(entries)} entries to {DEST}")
