"""
main.py — GovTrack Terminal Interface (CLI).

Entry point for all command-line operations on GovTrack.
Delegates to the appropriate module for each command.

Commands:
  python main.py init                      → create DB tables
  python main.py list                      → list all projects
  python main.py add-project               → add a project interactively
  python main.py add-rule  <project_id>    → add a governance rule
  python main.py add-member <project_id>   → add a team member manually
  python main.py list-members <project_id> → list all members of a project
  python main.py sync <project_id>         → fetch emails + meetings via Google APIs
  python main.py alerts <project_id>       → run governance check + print alerts
  python main.py show <project_id>         → print full project summary
  python main.py import-pdf <file.pdf>     → extract project details from PDF
"""

import re
import sys
from datetime import datetime, timezone
from govtrack.core.models import (
    init_db, Session, Project, GovernanceRule,
    Email, Meeting, Alert, Member
)
from govtrack.integrations.gmail_reader import fetch_emails
from govtrack.integrations.calendar_reader import fetch_meetings
from govtrack.services.alerts import run_governance
from govtrack.integrations.import_pdf import import_pdf


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def generate_project_id(db) -> str:
    """
    Generate the next sequential project ID in PRJ-XXXX format.
    Scans all existing IDs, finds the highest number, and increments it.
    Minimum output is PRJ-1000 (starts at 999 + 1).
    """
    existing = db.query(Project.project_id).all()
    max_num  = 999
    for (pid,) in existing:
        m = re.match(r"PRJ-(\d+)", (pid or "").upper())
        if m:
            max_num = max(max_num, int(m.group(1)))
    return f"PRJ-{max_num + 1:04d}"


# ══════════════════════════════════════════════════════════════════════════════
# INIT
# ══════════════════════════════════════════════════════════════════════════════

def cmd_init():
    """Create all database tables. Run once on first setup."""
    init_db()


# ══════════════════════════════════════════════════════════════════════════════
# LIST
# ══════════════════════════════════════════════════════════════════════════════

def cmd_list():
    """Print a summary table of all projects with health and progress."""
    db       = Session()
    projects = db.query(Project).all()

    if not projects:
        print("No projects yet. Run: python main.py add-project")
        db.close()
        return

    print(f"\n{'ID':<12} {'Name':<30} {'Client':<25} {'Health':<8} {'Progress'}")
    print("─" * 85)
    for p in projects:
        print(
            f"{p.project_id:<12} {p.name:<30} {p.client_name:<25} "
            f"{p.health.upper():<8} {p.delivery_pct}%"
        )
    db.close()


# ══════════════════════════════════════════════════════════════════════════════
# ADD PROJECT
# ══════════════════════════════════════════════════════════════════════════════

def cmd_add_project():
    """
    Interactively create a new project via terminal prompts.
    Project ID is auto-generated — the user only provides project details.
    """
    print("\n── Add New Project ──────────────────────────────")
    db         = Session()
    project_id = generate_project_id(db)
    print(f"System Project ID: {project_id}")

    p = Project(
        project_id       = project_id,
        name             = input("Project Name       (e.g. Retail ERP)         : ").strip(),
        client_name      = input("Client Name        (e.g. RetailMax India)    : ").strip(),
        description      = input("Description                                  : ").strip(),
        delivery_lead    = input("Delivery Lead      (e.g. Priya Sharma)       : ").strip(),
        engagement       = input("Engagement         (e.g. Fixed-Price 18mo)   : ").strip(),
        start_date       = datetime.strptime(
                               input("Start Date         (YYYY-MM-DD)              : ").strip(),
                               "%Y-%m-%d"
                           ),
        go_live_date     = datetime.strptime(
                               input("Go-Live Date       (YYYY-MM-DD)              : ").strip(),
                               "%Y-%m-%d"
                           ),
        delivery_pct     = float(input("Delivery % done    (0-100)                : ").strip() or 0),
        gmail_query      = input("Gmail query        (e.g. subject:PRJ-2041)   : ").strip(),
        calendar_keyword = input("Calendar keyword   (e.g. RetailMax)          : ").strip(),
    )

    db.add(p)
    db.commit()
    print(f"\n✅ Project {p.project_id} — {p.name} added successfully.")
    db.close()


# ══════════════════════════════════════════════════════════════════════════════
# ADD GOVERNANCE RULE
# ══════════════════════════════════════════════════════════════════════════════

def cmd_add_rule(project_id: str):
    """
    Interactively add a governance rule to an existing project.
    Supported rule types: MOM_SLA | WBR_SLA
    """
    db = Session()
    p  = db.query(Project).filter_by(project_id=project_id).first()
    if not p:
        print(f"❌ Project {project_id} not found.")
        db.close()
        return

    print(f"\n── Add Governance Rule for {project_id} ─────────────────")
    print("Rule types: MOM_SLA | WBR_SLA")

    rule = GovernanceRule(
        project_id  = p.id,
        rule_type   = input("Rule Type     : ").strip().upper(),
        description = input("Description   : ").strip(),
        sla_hours   = int(input("SLA hours (0 if N/A): ").strip() or 0),
        frequency   = input("Frequency (per_meeting / weekly / biweekly): ").strip(),
    )
    db.add(rule)
    db.commit()
    print(f"✅ Rule [{rule.rule_type}] added to {project_id}.")
    db.close()


# ══════════════════════════════════════════════════════════════════════════════
# ADD MEMBER
# ══════════════════════════════════════════════════════════════════════════════

def cmd_add_member(project_id: str):
    """
    Interactively add a team member to a project.
    If the email already exists, updates name and role instead of duplicating.
    """
    db = Session()
    p  = db.query(Project).filter_by(project_id=project_id).first()
    if not p:
        print(f"❌ Project {project_id} not found.")
        db.close()
        return

    print(f"\n── Add Team Member to {project_id} ──────────────────────")
    name  = input("Full Name  : ").strip()
    email = input("Email      : ").strip()
    role  = input("Role (e.g. Developer / BA / QA / PM / Client): ").strip() or "Team Member"

    # Upsert: update existing member if email already registered
    existing = db.query(Member).filter_by(project_id=p.id, email=email).first()
    if existing:
        print(f"⚠️  {email} is already a member ({existing.name} — {existing.role}). Updating...")
        existing.name = name
        existing.role = role
    else:
        db.add(Member(
            project_id = p.id,
            name       = name,
            email      = email,
            role       = role,
            source     = "manual",   # distinguishes from auto-extracted members
        ))

    db.commit()
    db.close()
    print(f"✅ Member {name} <{email}> [{role}] saved to {project_id}.")


# ══════════════════════════════════════════════════════════════════════════════
# LIST MEMBERS
# ══════════════════════════════════════════════════════════════════════════════

def cmd_list_members(project_id: str):
    """
    Print all team members for a project in a formatted table.
    Sorted by role — shows source (manual ✏️ or email 📧).
    """
    db = Session()
    p  = db.query(Project).filter_by(project_id=project_id).first()
    if not p:
        print(f"❌ Project {project_id} not found.")
        db.close()
        return

    members = db.query(Member).filter_by(project_id=p.id).order_by(Member.role).all()
    if not members:
        print(f"\n  No members found for {project_id}.")
        print(f"  Add manually : python main.py add-member {project_id}")
        print(f"  Auto-extract : python main.py sync {project_id}  (pulls from email senders)")
        db.close()
        return

    print(f"\n{'═'*65}")
    print(f"  TEAM MEMBERS — {p.project_id} ({p.name})")
    print(f"{'═'*65}")
    print(f"  {'Name':<25} {'Email':<35} {'Role':<20} {'Source'}")
    print(f"  {'─'*23} {'─'*33} {'─'*18} {'─'*8}")
    for m in members:
        print(f"  {m.name:<25} {m.email:<35} {m.role:<20} {m.source}")
    print(f"{'═'*65}")
    print(f"  Total: {len(members)} member(s)")
    db.close()


# ══════════════════════════════════════════════════════════════════════════════
# SYNC
# ══════════════════════════════════════════════════════════════════════════════

def cmd_sync(project_id: str):
    """
    Pull latest emails and calendar meetings for a project via Google APIs.
    Also auto-extracts new members from email senders after fetching.
    """
    db = Session()
    p  = db.query(Project).filter_by(project_id=project_id).first()
    db.close()

    if not p:
        print(f"❌ Project {project_id} not found.")
        return

    fetch_emails(p)
    fetch_meetings(p)
    _extract_members_from_emails(project_id)   # pull new senders as members


def _extract_members_from_emails(project_id: str):
    """
    Auto-extract unique email senders from synced emails and save them as members.
    Called after every sync to keep the member list current.

    Handles both "Display Name <email>" and bare "email" sender formats.
    Skips anyone already in the members table to avoid duplicates.
    """
    db     = Session()
    p      = db.query(Project).filter_by(project_id=project_id).first()
    if not p:
        db.close()
        return

    emails = db.query(Email).filter_by(project_id=p.id).all()
    added  = 0

    for e in emails:
        if not e.sender:
            continue

        # Parse "Display Name <email@domain.com>" format
        match = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>$', e.sender.strip())
        if match:
            name  = match.group(1).strip()
            email = match.group(2).strip().lower()
        elif "@" in e.sender:
            # Bare email address — derive name from local part
            email = e.sender.strip().lower()
            name  = email.split("@")[0].replace(".", " ").title()
        else:
            continue   # not a valid email — skip

        # Skip if already a member of this project
        if db.query(Member).filter_by(project_id=p.id, email=email).first():
            continue

        db.add(Member(
            project_id = p.id,
            name       = name,
            email      = email,
            role       = "Team Member",
            source     = "email",   # marks as auto-extracted, not manually added
        ))
        added += 1

    db.commit()
    db.close()

    if added:
        print(f"\n  👥 {added} new member(s) auto-extracted from email senders.")
        print(f"     Run: python main.py list-members {project_id}")


# ══════════════════════════════════════════════════════════════════════════════
# SHOW
# ══════════════════════════════════════════════════════════════════════════════

def cmd_show(project_id: str):
    """
    Print a full terminal summary for a project including:
    - Project metadata and governance rules
    - Team members (with source icons)
    - Email count by category with a bar chart
    - Latest 10 emails with risk flags
    - All meetings with MoM status (past vs upcoming)
    - All active (unresolved) alerts
    """
    db = Session()
    p  = db.query(Project).filter_by(project_id=project_id).first()
    if not p:
        print(f"❌ Project {project_id} not found.")
        db.close()
        return

    emails   = db.query(Email).filter_by(project_id=p.id).all()
    meetings = db.query(Meeting).filter_by(project_id=p.id).all()
    alerts   = db.query(Alert).filter_by(project_id=p.id, resolved=False).all()
    members  = db.query(Member).filter_by(project_id=p.id).order_by(Member.role).all()

    # Build email category → count map for the bar chart
    cats = {}
    for e in emails:
        cats[e.category] = cats.get(e.category, 0) + 1

    # ── Project header ────────────────────────────────────────────────────────
    print(f"""
╔══════════════════════════════════════════════════════════
║  {p.project_id} — {p.name}
╠══════════════════════════════════════════════════════════
║  Client        : {p.client_name}
║  Lead          : {p.delivery_lead}
║  Engagement    : {p.engagement}
║  Start         : {p.start_date.date() if p.start_date else '—'}
║  Go-Live       : {p.go_live_date.date() if p.go_live_date else '—'}
║  Progress      : {p.delivery_pct}%
║  Health        : {p.health.upper()}
║  Gmail Query   : {p.gmail_query}
║  Cal Keyword   : {p.calendar_keyword}
╠══ Governance Rules ({len(p.gov_rules)})""")

    for r in p.gov_rules:
        print(
            f"║    [{r.rule_type:<10}] {r.description} "
            f"| {r.frequency} | SLA {r.sla_hours}h | {r.status.upper()}"
        )

    # ── Team members ──────────────────────────────────────────────────────────
    print(f"╠══ Team Members ({len(members)})")
    if members:
        for m in members:
            # 📧 = auto-extracted from email  |  ✏️ = added manually
            src = "📧" if m.source == "email" else "✏️ "
            print(f"║    {src} {m.name:<25} {m.email:<35} {m.role}")
    else:
        print(f"║    No members yet. Run: python main.py add-member {project_id}")

    # ── Email category breakdown (ASCII bar chart) ────────────────────────────
    print(f"╠══ Emails ({len(emails)} total)")
    for cat, count in sorted(cats.items()):
        bar = "█" * count
        print(f"║    {cat:<12} {count:>3}  {bar}")

    # ── Latest 10 emails with risk flag ──────────────────────────────────────
    print(f"╠══ Recent Emails (latest 10)")
    for e in sorted(emails, key=lambda x: x.received_at, reverse=True)[:10]:
        risk = "⚠️ " if e.risk_signal else "   "
        print(f"║  {risk}[{e.category:<11}] {e.received_at.strftime('%b %d')}  {e.subject[:50]}")
        if e.summary:
            print(f"║             → {e.summary[:80]}")

    # ── Meetings with MoM status ──────────────────────────────────────────────
    print(f"╠══ Meetings ({len(meetings)} total)")
    now = datetime.now(timezone.utc).replace(tzinfo=None)   # naive UTC for comparison
    for m in sorted(meetings, key=lambda x: x.meeting_date, reverse=True):
        mom    = "✅" if m.mom_sent else "❌"
        status = "Past    " if m.meeting_date < now else "Upcoming"
        print(
            f"║    {status}  {m.meeting_date.strftime('%b %d %H:%M')}  "
            f"{m.title:<38} MoM:{mom}"
        )
        if m.summary:
            print(f"║              → {m.summary[:80]}")

    # ── Active alerts with colour icons ──────────────────────────────────────
    print(f"╠══ Active Alerts ({len(alerts)})")
    for a in alerts:
        icon = "🔴" if a.level == "red" else ("🟠" if a.level == "orange" else "🟢")
        print(f"║    {icon}  {a.title}")
        print(f"║        {a.description}")

    print("╚══════════════════════════════════════════════════════════")
    db.close()


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND ROUTER
# ══════════════════════════════════════════════════════════════════════════════

# Maps CLI command name → (handler_function, needs_project_id_arg)
# needs_arg=1 means the command requires a <project_id> as sys.argv[2]
# needs_arg=0 means the command takes no extra argument
COMMANDS = {
    "init":         (cmd_init,         0),
    "list":         (cmd_list,         0),
    "import-pdf":   (import_pdf,       1),   # delegates to import_pdf.py
    "add-project":  (cmd_add_project,  0),
    "add-rule":     (cmd_add_rule,     1),
    "add-member":   (cmd_add_member,   1),
    "list-members": (cmd_list_members, 1),
    "sync":         (cmd_sync,         1),
    "alerts":       (run_governance,   1),   # delegates to alerts.py
    "show":         (cmd_show,         1),
}

if __name__ == "__main__":
    # Print usage if no command given or command not recognised
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(0)

    cmd, needs_arg = COMMANDS[sys.argv[1]]

    if needs_arg:
        # Command requires a project_id argument — exit with usage hint if missing
        if len(sys.argv) < 3:
            print(f"Usage: python main.py {sys.argv[1]} <project_id>")
            sys.exit(1)
        cmd(sys.argv[2])
    else:
        cmd()