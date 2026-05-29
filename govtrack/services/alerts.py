"""
alerts.py — Governance check engine for GovTrack.

Reads a project's emails, meetings, and rules from the DB,
passes them to email_rules.py for alert generation, parses the
result, saves alerts to the DB, and updates project health.
"""

from datetime import datetime
from govtrack.core.models import Session, Project, Email, Meeting, Alert
from govtrack.ai.email_rules import generate_alerts


def run_governance(project_id: str):
    """
    Run a full governance check for the given project.

    Steps:
      1. Load emails, meetings, and rules from DB
      2. Format them as plain-text summaries
      3. Pass to generate_alerts() for keyword-based analysis
      4. Clear old unresolved alerts
      5. Parse the raw alert output and save new Alert rows
      6. Update project health (red / orange / green)
    """
    db = Session()

    # ── Step 1: Load project ──────────────────────────────────────────────────
    p = db.query(Project).filter_by(project_id=project_id).first()
    if not p:
        print(f"❌ Project {project_id} not found.")
        db.close()
        return

    # Fetch all emails and meetings for this project
    emails   = db.query(Email).filter_by(project_id=p.id).order_by(Email.received_at.desc()).all()
    meetings = db.query(Meeting).filter_by(project_id=p.id).order_by(Meeting.meeting_date.desc()).all()
    rules    = p.gov_rules

    # ── Step 2: Format as plain-text summaries ────────────────────────────────
    # These strings are passed to generate_alerts() for keyword scanning.
    # Fallback strings are used when no data exists — email_rules.py detects them
    # and raises "No Emails Synced" / "No Meetings Synced" alerts accordingly.

    emails_text = "\n".join(
        f"- [{e.category}] {e.received_at.strftime('%b %d')}: "
        f"{e.subject} | Risk: {'yes' if e.risk_signal else 'no'}"
        for e in emails
    ) or "No emails fetched yet."

    meetings_text = "\n".join(
        f"- {m.meeting_date.strftime('%b %d')}: {m.title} "
        f"({m.attendees} attendees) | MoM sent: {'yes' if m.mom_sent else 'no'}"
        for m in meetings
    ) or "No meetings fetched yet."

    rules_text = "\n".join(
        f"- [{r.rule_type}] {r.description} | Frequency: {r.frequency} | SLA: {r.sla_hours}h"
        for r in rules
    ) or "No governance rules defined."

    # ── Step 3: Generate alerts ───────────────────────────────────────────────
    print(f"\n🤖 Running governance check for {project_id}...")
    raw = generate_alerts(
        project_name     = p.name,
        client           = p.client_name,
        emails_summary   = emails_text,
        meetings_summary = meetings_text,
        gov_rules        = rules_text,
    )

    # ── Step 4: Clear previous unresolved alerts ──────────────────────────────
    # Always start fresh so stale alerts don't persist after issues are fixed
    db.query(Alert).filter_by(project_id=p.id, resolved=False).delete()
    db.commit()

    # Print raw output to terminal for visibility during CLI usage
    print(f"\n{'═'*60}")
    print(f"  GOVERNANCE ALERTS — {p.project_id} ({p.name})")
    print(f"{'═'*60}")
    print(raw)
    print(f"{'═'*60}")

    # ── Step 5: Parse alert blocks and save to DB ─────────────────────────────
    # Raw output from email_rules.py is split by "---" into individual alert blocks.
    # Each block contains: ALERT [LEVEL], Title: ..., Description: ...
    for block in raw.strip().split("---"):
        block = block.strip()
        if not block:
            continue

        # Default level is orange — overwritten if RED or GREEN is found in block
        level, title, description = "orange", "", ""

        for line in block.splitlines():
            line = line.strip()
            if   "ALERT [RED]"    in line: level = "red"
            elif "ALERT [GREEN]"  in line: level = "green"
            elif "ALERT [ORANGE]" in line: level = "orange"
            elif line.lower().startswith("title:"):
                title = line.split(":", 1)[1].strip()
            elif line.lower().startswith("description:"):
                description = line.split(":", 1)[1].strip()

        # Only save if a title was found — skips malformed or empty blocks
        if title:
            db.add(Alert(
                project_id  = p.id,
                level       = level,
                title       = title,
                description = description,
            ))

    # ── Step 6: Update project health ────────────────────────────────────────
    # Health is derived from the worst active alert level:
    #   any RED → red | any ORANGE → orange | otherwise → green
    all_alerts = db.query(Alert).filter_by(project_id=p.id, resolved=False).all()

    if any(a.level == "red" for a in all_alerts):
        p.health = "red"
    elif any(a.level == "orange" for a in all_alerts):
        p.health = "orange"
    else:
        p.health = "green"

    db.commit()

    # Read health into a local variable before closing the session —
    # accessing p.health after db.close() raises DetachedInstanceError
    health = p.health
    db.close()

    print(f"\n  Project health updated → {health.upper()}")
