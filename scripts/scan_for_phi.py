#!/usr/bin/env python3
"""
Retroactive PHI Scan Tool for LitPulse.
Scans existing discussion_threads, discussion_comments, and notes
for potential PHI using phi_guard patterns.
Outputs a JSON report — no destructive actions.
"""
import sys
import os
import json
from datetime import datetime, timezone

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from utils.phi_guard import scan_for_phi


def main():
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "litpulse_db")
    client = MongoClient(mongo_url)
    db = client[db_name]

    report = {
        "scan_time": datetime.now(timezone.utc).isoformat(),
        "findings": [],
        "summary": {"threads": 0, "comments": 0, "notes": 0, "total_flagged": 0},
    }

    # Scan thread titles
    for doc in db.discussion_threads.find({}, {"_id": 0, "thread_id": 1, "title": 1, "created_by": 1}):
        hits = scan_for_phi(doc.get("title", ""))
        if hits:
            report["findings"].append({
                "collection": "discussion_threads",
                "document_id": doc["thread_id"],
                "field": "title",
                "user_id": doc.get("created_by"),
                "detected": [h["type"] for h in hits],
            })
            report["summary"]["threads"] += 1

    # Scan comment bodies (non-deleted only)
    for doc in db.discussion_comments.find({"deleted_at": None}, {"_id": 0, "comment_id": 1, "body": 1, "user_id": 1}):
        hits = scan_for_phi(doc.get("body", ""))
        if hits:
            report["findings"].append({
                "collection": "discussion_comments",
                "document_id": doc["comment_id"],
                "field": "body",
                "user_id": doc.get("user_id"),
                "detected": [h["type"] for h in hits],
            })
            report["summary"]["comments"] += 1

    # Scan notes
    for doc in db.notes.find({}, {"_id": 0, "note_id": 1, "body": 1, "user_id": 1}):
        hits = scan_for_phi(doc.get("body", ""))
        if hits:
            report["findings"].append({
                "collection": "notes",
                "document_id": doc["note_id"],
                "field": "body",
                "user_id": doc.get("user_id"),
                "detected": [h["type"] for h in hits],
            })
            report["summary"]["notes"] += 1

    report["summary"]["total_flagged"] = len(report["findings"])

    # Write report
    out_path = os.path.join(os.path.dirname(__file__), "..", "..", "test_reports", "phi_scan.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)

    print(f"PHI Scan complete. {report['summary']['total_flagged']} items flagged.")
    print(f"Report written to: {out_path}")


if __name__ == "__main__":
    main()
