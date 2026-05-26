"""
fix_projects_db.py — Run this ONCE on your machine to fix two things:

1. Creates PRJ-3773 (Flipkart) in the DB so name-matching works
2. Fixes all auto-created projects that have "Unknown Client" and
   bare "PRJ-XXXX" as calendar_keyword — updates them from their
   saved emails so _match_project_by_name can find them.

Usage:
    python fix_projects_db.py
"""

import sys, os, re
sys.path.insert(0, os.path.dirname(__file__))

from govtrack.core.models import Session, Project, GovernanceRule, Email
from datetime import datetime

DEFAULT_RULES = [
    ("MOM_SLA",  "Minutes of Meeting must be sent after every meeting", 48, "per_meeting"),
    ("WBR_SLA",  "Weekly Business Review report",                       24, "weekly"),

]

GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "icloud.com", "protonmail.com", "me.com", "live.com",
    "rediffmail.com", "ymail.com",
}


def _infer_client_from_email(sender: str) -> str:
    if not sender or "@" not in sender:
        return ""
    domain = sender.split("@")[-1].lower()
    if domain in GENERIC_DOMAINS:
        return ""
    return domain.split(".")[0].replace("-", " ").title()


def _extract_name_from_subject(subject: str, pid: str) -> str:
    if not subject:
        return ""
    cleaned = re.sub(r"[\[\(]?" + re.escape(pid) + r"[\]\)]?", "", subject, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^[\s|\-:–—]+", "", cleaned).strip()
    cleaned = re.sub(r"[\s|\-:–—]+$", "", cleaned).strip()
    noise = {"update", "re", "fwd", "fw", "follow up", "follow-up", "hi", "hello"}
    return cleaned if cleaned and len(cleaned) >= 4 and cleaned.lower() not in noise else ""


# ── 1. Create PRJ-3773 if missing ─────────────────────────────────────────────

db = Session()

existing = db.query(Project).filter_by(project_id="PRJ-3773").first()
if existing:
    print("PRJ-3773 already exists — updating client_name and gmail_query...")
    existing.client_name      = "Flipkart"
    existing.calendar_keyword = "Flipkart"
    existing.gmail_query      = 'PRJ-3773'   # bare ID; query builder adds subject:Flipkart automatically
    if not existing.name or existing.name.startswith("Project PRJ"):
        existing.name = "Flipkart Ads"
else:
    p = Project(
        project_id       = "PRJ-3773",
        name             = "Flipkart Ads",
        client_name      = "Flipkart",
        description      = "Flipkart ads project",
        delivery_lead    = "Gulshan Likhar",
        engagement       = "",
        gmail_query      = 'PRJ-3773',   # query builder adds subject:Flipkart automatically
        calendar_keyword = "Flipkart",
        delivery_pct     = 0,
        health           = "green",
        pm_confirmed     = False,
    )
    db.add(p)
    db.flush()
    for rule_type, desc, sla, freq in DEFAULT_RULES:
        db.add(GovernanceRule(
            project_id  = p.id,
            rule_type   = rule_type,
            description = desc,
            sla_hours   = sla,
            frequency   = freq,
        ))
    print("✅ PRJ-3773 (Flipkart) created")

db.commit()


# ── 2. Fix all "Unknown Client" projects using their saved emails ──────────────

projects = db.query(Project).filter(
    Project.client_name.in_(["Unknown Client", "", None])
).all()

print(f"\nFixing {len(projects)} projects with Unknown Client...")

for p in projects:
    emails = db.query(Email).filter_by(project_id=p.id).order_by(Email.received_at).all()
    if not emails:
        print(f"  {p.project_id} — no emails, skipping")
        continue

    # Try to infer client from sender domains
    client = ""
    for e in emails:
        client = _infer_client_from_email(e.sender or "")
        if client:
            break

    # Try to infer project name from earliest email subject
    name = ""
    for e in emails:
        name = _extract_name_from_subject(e.subject or "", p.project_id)
        if name:
            break

    updated = []
    if client:
        p.client_name      = client
        p.calendar_keyword = client  # use client as keyword so name-matching works
        updated.append(f"client='{client}'")

    if name and (not p.name or p.name.startswith("Project PRJ")):
        p.name = name
        updated.append(f"name='{name}'")

    # gmail_query: just store the project ID; _build_gmail_query adds subject:client automatically
    if not p.gmail_query or p.gmail_query.strip() == "":
        p.gmail_query = p.project_id
        updated.append("gmail_query set to project_id")

    if updated:
        print(f"  ✅ {p.project_id} → {', '.join(updated)}")
    else:
        print(f"  — {p.project_id} nothing to update")

db.commit()
db.close()
print("\n✅ Done. Restart the watcher for changes to take effect.")
print("   python main.py  (or restart your Streamlit app)")
