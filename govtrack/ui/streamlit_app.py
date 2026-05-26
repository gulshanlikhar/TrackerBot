"""
streamlit_app.py — GovTrack Web Dashboard.

Main Streamlit UI entry point. Renders all dashboard pages and
starts the background email watcher thread on load.

Install : pip install streamlit sqlalchemy python-dotenv pypdf
Run     : streamlit run streamlit_app.py

Pages:
  Dashboard    → project overview, stats, MoM timers, alerts
  Emails       → filterable email list with thread links
  Attach Emails → manually assign unmapped emails to projects
  Meetings     → upcoming and past meetings with MoM status
  Alerts       → governance alerts + manual check trigger
  Members      → team member list grouped by role
  Add Project  → create a new project via form
  Add Member   → add a team member to a project
  Import PDF   → extract project details from a PDF brief
"""

import os
import sys
import re
import tempfile
from datetime import datetime, timezone

import streamlit as st
from dotenv import load_dotenv
from govtrack.core.paths import ENV_PATH

load_dotenv(ENV_PATH)

# Allow imports from the app's own directory
sys.path.insert(0, os.path.dirname(__file__))

from govtrack.core.models import (
    init_db, Session, Project, GovernanceRule,
    Email, Meeting, Alert, UnmappedEmail
)

# Member and migrate_db were added in a later schema version.
# Guard import so the app still runs on older DB setups.
try:
    from govtrack.core.models import Member, migrate_db
    HAS_MEMBERS = True
except ImportError:
    HAS_MEMBERS = False
    def migrate_db(): pass   # no-op fallback


# ══════════════════════════════════════════════════════════════════════════════
# BACKGROUND EMAIL WATCHER
# ══════════════════════════════════════════════════════════════════════════════

# Start the inbox watcher in a background thread when the dashboard loads.
# WATCHER_OK is shown as a status dot in the sidebar.
try:
    from govtrack.services.Email_watcher import start_watcher, watcher_running
    if not watcher_running():
        start_watcher()
    WATCHER_OK    = True
    WATCHER_ERROR = ""
except Exception as watcher_error:
    WATCHER_OK    = False
    WATCHER_ERROR = str(watcher_error)


# ══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════════════════

# Create tables if they don't exist, then safely add any missing columns
init_db()
migrate_db()

# Only MOM_SLA and WBR_SLA are valid rule types — remove any others on startup
ALLOWED_RULE_TYPES = ("MOM_SLA", "WBR_SLA")

def cleanup_governance_rules():
    """Delete any governance rules with unsupported rule types from the DB."""
    s = Session()
    try:
        s.query(GovernanceRule).filter(
            ~GovernanceRule.rule_type.in_(ALLOWED_RULE_TYPES)
        ).delete(synchronize_session=False)
        s.commit()
    except Exception:
        s.rollback()
    finally:
        s.close()

cleanup_governance_rules()


# ══════════════════════════════════════════════════════════════════════════════
# MAGIC LINK ROUTING
# ══════════════════════════════════════════════════════════════════════════════

# If the URL contains ?confirm_token=..., render the PM confirmation page
# instead of the main dashboard. This is triggered by clicking the email link.
_query_params = st.query_params
if "confirm_token" in _query_params:
    from govtrack.ui.Confirm_page import render_confirm_page
    render_confirm_page(_query_params["confirm_token"])
    st.stop()   # stop here — don't render the dashboard


# ══════════════════════════════════════════════════════════════════════════════
# PAGE CONFIG & THEME
# ══════════════════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="GovTrack",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Global dark-theme CSS — overrides Streamlit defaults and injects custom
# component classes used throughout the dashboard (scard, badge, alert-block, etc.)
st.markdown("""
<style>
html, body, [data-testid="stAppViewContainer"] {
    background:#0E1117 !important; color:#E8EAED !important;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
}
[data-testid="stSidebar"] { background:#151922 !important; border-right:1px solid #2A303A !important; }
.block-container { padding:1.5rem 2rem 3rem !important; max-width:1400px; }
#MainMenu,footer,header { visibility:hidden; }

/* ── Stat cards (top row of Dashboard) ── */
.stat-card  { background:#171B24; border:1px solid #2A303A; border-radius:12px; padding:1.1rem 1.3rem 1rem; box-shadow:0 12px 30px rgba(0,0,0,.18); }
.stat-label { font-size:11px; font-weight:500; letter-spacing:.06em; text-transform:uppercase; color:#9AA4B2; }
.stat-value { font-size:26px; font-weight:600; color:#F5F7FA; margin:4px 0 2px; line-height:1; }
.stat-sub   { font-size:12px; color:#9AA4B2; }

/* ── Section cards ── */
.scard       { background:#171B24; border:1px solid #2A303A; border-radius:12px; padding:1.2rem 1.4rem; margin-bottom:.75rem; box-shadow:0 12px 30px rgba(0,0,0,.16); }
.scard-title { font-size:13px; font-weight:600; color:#F5F7FA; margin-bottom:.9rem; }

/* ── Row items inside cards ── */
.row-item           { display:flex; align-items:flex-start; gap:11px; padding:9px 0; border-bottom:1px solid #252B35; }
.row-item:last-child{ border-bottom:none; }
.row-icon  { width:30px; height:30px; border-radius:7px; display:flex; align-items:center; justify-content:center; font-size:14px; flex-shrink:0; margin-top:1px; }
.row-title { font-size:13px; font-weight:500; color:#F5F7FA; }
.row-sub   { font-size:12px; color:#AAB3C2; margin-top:2px; line-height:1.55; }

/* ── Colour badges ── */
.badge    { display:inline-block; font-size:11px; font-weight:500; padding:2px 9px; border-radius:20px; line-height:1.6; white-space:nowrap; }
.b-green  { background:#123C2A; color:#8EE6B2; }
.b-orange { background:#422A12; color:#F6C177; }
.b-red    { background:#461B24; color:#FF9AA8; }
.b-blue   { background:#172E55; color:#9FC3FF; }
.b-purple { background:#31235E; color:#C8B6FF; }
.b-teal   { background:#123C3B; color:#8CE6DD; }
.b-amber  { background:#443512; color:#F6D36F; }
.b-gray   { background:#2B3039; color:#C5CAD3; }

/* ── Alert blocks ── */
.alert-block { border-radius:10px; padding:.9rem 1.1rem; margin-bottom:9px; }
.al-red      { background:#2A151B; border-left:4px solid #E55363; }
.al-orange   { background:#2B2112; border-left:4px solid #F6A623; }
.al-green    { background:#12281D; border-left:4px solid #38A169; }
.al-title    { font-size:13px; font-weight:600; color:#F5F7FA; margin-bottom:3px; }
.al-desc     { font-size:12px; color:#D8DDE6; }
.al-time     { font-size:11px; color:#9AA4B2; margin-top:5px; }

/* ── Progress bar ── */
.prog-wrap  { background:#2B3039; border-radius:99px; height:7px; width:100%; }
.prog-fill  { height:7px; border-radius:99px; }
.prog-label { font-size:12px; color:#AAB3C2; margin-top:5px; }

/* ── Member avatar initials circle ── */
.avatar { width:32px; height:32px; border-radius:50%; background:#31235E; color:#D8CCFF; display:flex; align-items:center; justify-content:center; font-size:11px; font-weight:600; flex-shrink:0; }

/* ── Health indicator dot ── */
.hdot        { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:5px; vertical-align:middle; }
.hd-green    { background:#38A169; }
.hd-orange   { background:#F6A623; }
.hd-red      { background:#E53E3E; }

/* ── Page title and subtitle ── */
.page-title { font-size:22px; font-weight:600; color:#F5F7FA; margin-bottom:.2rem; }
.page-sub   { font-size:13px; color:#AAB3C2; margin-bottom:1.2rem; }
.div        { border-top:1px solid #2A303A; margin:1rem 0; }

/* ── Mini data table ── */
.mini-table    { width:100%; border-collapse:collapse; font-size:12px; }
.mini-table th { text-align:left; color:#9AA4B2; font-weight:500; padding:4px 8px 8px; border-bottom:1px solid #2A303A; font-size:11px; letter-spacing:.05em; text-transform:uppercase; }
.mini-table td { padding:7px 8px; border-bottom:1px solid #252B35; color:#E8EAED; vertical-align:middle; }
.mini-table tr:last-child td { border-bottom:none; }

a { color:#8AB4F8 !important; }

/* ── Sidebar overrides ── */
[data-testid="stSidebar"] * { color:#D8DDE6 !important; }
[data-testid="stSidebar"] button { background:#1D2330 !important; border:1px solid #303847 !important; color:#E8EAED !important; }
[data-testid="stSidebar"] button:hover { border-color:#6EA8FE !important; color:#FFFFFF !important; }

/* ── Button overrides ── */
.stButton button             { background:#1D2330 !important; border:1px solid #303847 !important; color:#E8EAED !important; border-radius:8px !important; }
.stButton button:hover       { border-color:#6EA8FE !important; color:#FFFFFF !important; }
.stButton button[kind="primary"] { background:#2F6FED !important; border-color:#2F6FED !important; color:#FFFFFF !important; }

/* ── Form input overrides ── */
input, textarea, [data-baseweb="select"] > div { background:#111722 !important; color:#E8EAED !important; border-color:#303847 !important; }
label, .stMarkdown, .stTextInput, .stTextArea, .stSelectbox, .stDateInput, .stNumberInput, .stSlider { color:#E8EAED !important; }
hr { border-color:#2A303A !important; }

/* ── Notifier HTML colour overrides (emails render light-theme HTML) ── */
[style*="#1A1A18"] { color:#F5F7FA !important; }
[style*="#3A3A34"] { color:#D8DDE6 !important; }
[style*="#6C6B63"], [style*="#8A8980"] { color:#9AA4B2 !important; }
[style*="#F0EFE9"] { background:#242B36 !important; border-color:#2A303A !important; }
[style*="#E5E4DE"] { border-color:#2A303A !important; }
[style*="#FFFFFF"] { background:#171B24 !important; color:#E8EAED !important; }
[style*="#F5F5F0"] { background:#0E1117 !important; }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# SHARED HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def db():
    """Open and return a new SQLAlchemy session. Caller must close it."""
    return Session()


def generate_project_id(session) -> str:
    """Generate the next sequential PRJ-XXXX project ID. Minimum PRJ-1000."""
    existing = session.query(Project.project_id).all()
    max_num  = 999
    for (pid,) in existing:
        m = re.match(r"PRJ-(\d+)", (pid or "").upper())
        if m:
            max_num = max(max_num, int(m.group(1)))
    return f"PRJ-{max_num + 1:04d}"


def gmail_thread_url(thread_id: str) -> str:
    """Build a Gmail deep-link URL for a thread ID. Returns "" if no ID."""
    return f"https://mail.google.com/mail/u/0/#inbox/{thread_id}" if thread_id else ""


def create_default_rules(session, project):
    """
    Add MOM_SLA and WBR_SLA rules to a project if they don't already exist.
    Called after every project creation to ensure governance rules are always set.
    """
    existing = {
        r.rule_type for r in session.query(GovernanceRule).filter_by(project_id=project.id).all()
    }
    defaults = [
        ("MOM_SLA", "Minutes of Meeting must be sent after every meeting", 8, "per_meeting"),
        ("WBR_SLA", "Weekly Business Review report",                       24, "weekly"),
    ]
    for rule_type, description, sla_hours, frequency in defaults:
        if rule_type not in existing:
            session.add(GovernanceRule(
                project_id  = project.id,
                rule_type   = rule_type,
                description = description,
                sla_hours   = sla_hours,
                frequency   = frequency,
            ))


def attach_unmapped_to_project(session, unmapped, target) -> bool:
    """
    Classify and save an UnmappedEmail as a regular Email on the target project.
    Also marks the UnmappedEmail as assigned.

    Returns True if a new Email row was created, False if it already existed.
    """
    from govtrack.ai.gemini import classify_email, summarize_email

    # Prefix with project ID so the same Gmail message can exist on multiple projects
    storage_msg_id = f"{unmapped.gmail_msg_id}:{target.project_id}"

    existing = session.query(Email).filter(
        Email.project_id == target.id,
        Email.gmail_msg_id.in_([unmapped.gmail_msg_id, storage_msg_id]),
    ).first()

    if not existing:
        content  = unmapped.body_preview or unmapped.snippet or ""
        subject  = unmapped.subject or "(no subject)"
        category, risk = classify_email(subject, content)
        summary        = summarize_email(subject, content, category)
        session.add(Email(
            project_id      = target.id,
            gmail_msg_id    = storage_msg_id,
            gmail_thread_id = getattr(unmapped, "gmail_thread_id", "") or unmapped.gmail_msg_id,
            subject         = subject,
            sender          = unmapped.sender or "",
            received_at     = unmapped.received_at or datetime.utcnow(),
            category        = category,
            risk_signal     = risk,
            snippet         = unmapped.snippet or "",
            summary         = summary,
            match_signal    = "manual_attach",   # indicates manager manually assigned this
        ))

    # Mark the unmapped record as assigned regardless of whether Email was new
    unmapped.assigned_project_id = target.id
    unmapped.assigned_at         = datetime.utcnow()
    unmapped.assigned_by         = "manual"
    return existing is None


# ── HTML snippet helpers ──────────────────────────────────────────────────────

def health_html(h: str) -> str:
    """Return a coloured badge HTML string for a project health value (green/orange/red)."""
    h   = (h or "green").lower()
    dot = {"green": "hd-green", "orange": "hd-orange", "red": "hd-red"}.get(h, "hd-green")
    bc  = {"green": "b-green",  "orange": "b-orange",  "red": "b-red" }.get(h, "b-gray")
    lbl = {"green": "Healthy",  "orange": "At Risk",   "red": "Critical"}.get(h, h.title())
    return f'<span class="badge {bc}"><span class="hdot {dot}"></span>{lbl}</span>'


def cat_html(cat: str) -> str:
    """Return a coloured badge HTML string for an email category."""
    m = {"MoM": "b-teal", "WBR": "b-blue", "Delay": "b-orange", "Escalation": "b-red", "General": "b-gray"}
    return f'<span class="badge {m.get(cat, "b-gray")}">{cat}</span>'


def rule_html(rt: str) -> str:
    """Return a coloured badge HTML string for a governance rule type."""
    m = {"MOM_SLA": "b-teal", "WBR_SLA": "b-blue"}
    return f'<span class="badge {m.get(rt, "b-gray")}">{rt}</span>'


def initials(name: str) -> str:
    """Extract 2-letter initials from a full name for the member avatar circle."""
    p = name.strip().split()
    return (p[0][0] + p[-1][0]).upper() if len(p) >= 2 else name[:2].upper()


def prog_bar(pct: float) -> str:
    """Return an HTML progress bar for a delivery percentage (0–100)."""
    pct = min(max(float(pct), 0), 100)
    return (
        f'<div class="prog-wrap">'
        f'<div class="prog-fill" style="width:{pct:.0f}%;background:#5C3FD4"></div>'
        f'</div>'
        f'<div class="prog-label">{pct:.0f}% complete</div>'
    )


def load(pid: str) -> tuple:
    """
    Load all data for a project in one DB call.
    Returns (project, emails, meetings, alerts, members, rules).
    Returns (None, [], [], [], [], []) if project not found.
    """
    s = db()
    p = s.query(Project).filter_by(project_id=pid).first()
    if not p:
        s.close()
        return None, [], [], [], [], []

    emails   = s.query(Email).filter_by(project_id=p.id).order_by(Email.received_at.desc()).all()
    meetings = s.query(Meeting).filter_by(project_id=p.id).order_by(Meeting.meeting_date.desc()).all()
    alerts   = s.query(Alert).filter_by(project_id=p.id, resolved=False).all()
    members  = (
        s.query(Member).filter_by(project_id=p.id).order_by(Member.role).all()
        if HAS_MEMBERS else []
    )
    # Only load allowed rule types — keeps UI clean even if DB has stale rules
    rules = s.query(GovernanceRule).filter(
        GovernanceRule.project_id == p.id,
        GovernanceRule.rule_type.in_(ALLOWED_RULE_TYPES),
    ).all()
    s.close()
    return p, emails, meetings, alerts, members, rules


def check_mom_timers(project, meetings: list) -> list:
    """
    Check MoM SLA status for all past meetings without MoM sent.

    For each overdue meeting:
      - Creates a red Alert if not already present
      - Fires a send_mom_overdue_alert email (once per meeting via mom_alert_sent flag)
      - Updates project health to red

    Returns a list of (meeting, hours_remaining, is_overdue) tuples
    for rendering the MoM countdown UI on the Dashboard.
    """
    now    = datetime.now()
    timers = []
    s      = db()
    proj   = s.query(Project).filter_by(project_id=project.project_id).first()

    for m in meetings:
        # Skip meetings that already have MoM sent or no deadline set
        if m.mom_sent or not m.mom_deadline:
            continue
        # Only track past meetings — future ones haven't happened yet
        if m.meeting_date > now:
            continue

        hours_remaining = (m.mom_deadline - now).total_seconds() / 3600
        is_overdue      = hours_remaining < 0

        if is_overdue:
            # Create alert only if one doesn't already exist for this meeting
            existing = s.query(Alert).filter_by(
                project_id = proj.id,
                title      = f"MoM Overdue: {m.title[:40]}",
                resolved   = False,
            ).first()
            if not existing:
                s.add(Alert(
                    project_id  = proj.id,
                    level       = "red",
                    title       = f"MoM Overdue: {m.title[:40]}",
                    description = f"Minutes of Meeting not sent. SLA breached by {abs(hours_remaining):.1f}h.",
                ))
                proj.health = "red"
                s.commit()

                # Fire overdue email once — mom_alert_sent flag prevents resending
                meet_obj = s.query(Meeting).filter_by(id=m.id).first()
                if meet_obj and not meet_obj.mom_alert_sent and proj.pm_email:
                    try:
                        from govtrack.services.Notifier import send_mom_overdue_alert
                        send_mom_overdue_alert(proj, meet_obj, proj.pm_email, abs(hours_remaining))
                        meet_obj.mom_alert_sent = True
                        s.commit()
                    except Exception as e:
                        print(f"MoM alert email failed: {e}")

        timers.append((m, hours_remaining, is_overdue))

    s.close()
    return timers


def run_alerts(p, emails: list, meetings: list, rules: list):
    """
    Run a fresh governance check for a project and save the results.

    Generates alert blocks from gemini.py, clears old unresolved alerts,
    parses the new blocks, saves them, and updates project health.
    Called from the Alerts page when the user clicks 'Run governance check'.
    """
    from govtrack.ai.gemini import generate_alerts

    # Build plain-text summaries for gemini.py to analyse
    et = "\n".join(
        f"- [{e.category}] {e.received_at.strftime('%b %d')}: "
        f"{e.subject} | Risk: {'yes' if e.risk_signal else 'no'}"
        for e in emails
    ) or "No emails fetched yet."

    mt = "\n".join(
        f"- {m.meeting_date.strftime('%b %d')}: {m.title} "
        f"({m.attendees} attendees) | MoM sent: {'yes' if m.mom_sent else 'no'}"
        for m in meetings
    ) or "No meetings fetched yet."

    rt = "\n".join(
        f"- [{r.rule_type}] {r.description} | {r.frequency} | SLA {r.sla_hours}h"
        for r in rules
    ) or "No rules defined."

    raw  = generate_alerts(p.name, p.client_name, et, mt, rt)
    s    = db()
    proj = s.query(Project).filter_by(project_id=p.project_id).first()

    # Clear old alerts before saving fresh ones
    s.query(Alert).filter_by(project_id=proj.id, resolved=False).delete()
    s.commit()

    # Parse each "---" separated alert block from the raw output
    for block in raw.strip().split("---"):
        block = block.strip()
        if not block:
            continue
        level, title, desc = "orange", "", ""
        for line in block.splitlines():
            line = line.strip()
            if   "ALERT [RED]"    in line: level = "red"
            elif "ALERT [GREEN]"  in line: level = "green"
            elif "ALERT [ORANGE]" in line: level = "orange"
            elif line.lower().startswith("title:"):       title = line.split(":", 1)[1].strip()
            elif line.lower().startswith("description:"): desc  = line.split(":", 1)[1].strip()
        if title:
            s.add(Alert(project_id=proj.id, level=level, title=title, description=desc))

    # Update health based on worst active alert level
    all_a       = s.query(Alert).filter_by(project_id=proj.id, resolved=False).all()
    proj.health = (
        "red"    if any(a.level == "red"    for a in all_a) else
        "orange" if any(a.level == "orange" for a in all_a) else
        "green"
    )
    s.commit()
    s.close()


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown(
        '<div style="font-size:18px;font-weight:700;color:#1A1A18;padding:0 4px 2px">🛡️ GovTrack</div>',
        unsafe_allow_html=True
    )
    st.markdown(
        '<div style="font-size:11px;color:#8A8980;padding:0 4px 10px">Delivery Governance Platform</div>',
        unsafe_allow_html=True
    )
    st.markdown('<div class="div"></div>', unsafe_allow_html=True)

    # ── Project selector ──────────────────────────────────────────────────────
    s        = db()
    projects = s.query(Project).all()
    s.close()

    if projects:
        pm          = {p.project_id: p for p in projects}
        selected_id = st.selectbox(
            "Project",
            [p.project_id for p in projects],
            format_func = lambda pid: f"{pid} — {pm[pid].name[:18]}",
            label_visibility = "collapsed",
        )
    else:
        selected_id = None
        st.markdown(
            '<div style="font-size:12px;color:#8A8980;padding:4px">No projects yet.</div>',
            unsafe_allow_html=True
        )

    st.markdown('<div class="div"></div>', unsafe_allow_html=True)

    # ── Page navigation buttons ───────────────────────────────────────────────
    if "page" not in st.session_state:
        st.session_state.page = "Dashboard"

    pages = [
        ("Dashboard",    "📊"),
        ("Emails",       "📧"),
        ("Attach Emails","🔗"),
        ("Meetings",     "📅"),
        ("Alerts",       "🔔"),
        ("Members",      "👥"),
        ("Add Project",  "➕"),
        ("Add Member",   "👤"),
        ("Import PDF",   "📄"),
    ]
    for label, icon in pages:
        if st.button(f"{icon}  {label}", key=f"nav_{label}", use_container_width=True):
            st.session_state.page = label
            st.rerun()

    # ── Health badge for selected project ────────────────────────────────────
    if selected_id:
        pc, *_ = load(selected_id)
        if pc:
            st.markdown('<div class="div"></div>', unsafe_allow_html=True)
            st.markdown(
                f'<div style="padding:2px;font-size:12px;color:#6C6B63">'
                f'Health: {health_html(pc.health)}</div>',
                unsafe_allow_html=True
            )

    # ── Watcher status indicator ──────────────────────────────────────────────
    st.markdown('<div class="div"></div>', unsafe_allow_html=True)
    watcher_dot = "🟢" if WATCHER_OK else "🔴"
    st.markdown(
        f'<div style="font-size:11px;color:#8A8980;padding:2px 4px">'
        f'{watcher_dot} Email watcher {"active" if WATCHER_OK else "inactive"}</div>',
        unsafe_allow_html=True
    )
    if WATCHER_ERROR:
        st.caption(f"Watcher error: {WATCHER_ERROR}")

page = st.session_state.page


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

if page == "Dashboard":
    if not selected_id:
        st.markdown('<div class="page-title">Welcome to GovTrack 🛡️</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="page-sub">No projects yet. Use <b>Import PDF</b> or <b>Add Project</b> to get started.</div>',
            unsafe_allow_html=True
        )
        st.stop()

    p, emails, meetings, alerts, members, rules = load(selected_id)

    # ── Header row ────────────────────────────────────────────────────────────
    hc, bc = st.columns([8, 2])
    with hc:
        st.markdown(f'<div class="page-title">{p.project_id} — {p.name}</div>', unsafe_allow_html=True)
        st.markdown(
            f'<div class="page-sub">Client: <b>{p.client_name}</b> &nbsp;·&nbsp; '
            f'Lead: <b>{p.delivery_lead}</b> &nbsp;·&nbsp; {p.engagement}</div>',
            unsafe_allow_html=True
        )
    with bc:
        st.markdown(
            f'<div style="text-align:right;padding-top:1.1rem">{health_html(p.health)}</div>',
            unsafe_allow_html=True
        )

    # ── PM confirmation status banner ─────────────────────────────────────────
    # Read-only — PM confirms via the magic link in their email, not here.
    if not p.pm_confirmed:
        st.markdown("""
        <div style="background:#FEF0DA;border:1px solid #F6A623;border-radius:10px;
                    padding:12px 20px;margin-bottom:1rem;display:flex;align-items:center;gap:12px">
          <span style="font-size:18px">⏳</span>
          <div>
            <div style="font-size:13px;font-weight:600;color:#7A4210">PM Confirmation Pending</div>
            <div style="font-size:12px;color:#7A4210;margin-top:2px">
              A confirmation email has been sent to the Project Manager. Awaiting their response.
            </div>
          </div>
        </div>""", unsafe_allow_html=True)
    else:
        st.markdown("""
        <div style="background:#D6F0E0;border:1px solid #A3D9B8;border-radius:10px;
                    padding:12px 20px;margin-bottom:1rem;display:flex;align-items:center;gap:12px">
          <span style="font-size:18px">✅</span>
          <div style="font-size:13px;font-weight:600;color:#1A5C38">
            PM Confirmed — All project details verified by the Project Manager
          </div>
        </div>""", unsafe_allow_html=True)

    # ── Stat cards row ─────────────────────────────────────────────────────────
    c1, c2, c3, c4, c5 = st.columns(5)
    for col, label, value, sub in [
        (c1, "Progress", f"{p.delivery_pct:.0f}%", "delivery done"),
        (c2, "Emails",   len(emails),   f"{sum(1 for e in emails if e.risk_signal)} risk signals"),
        (c3, "Meetings", len(meetings), f"{sum(1 for m in meetings if not m.mom_sent)} MoM missing"),
        (c4, "Alerts",   len(alerts),   f"{sum(1 for a in alerts if a.level == 'red')} critical"),
        (c5, "Members",  len(members),  "team size"),
    ]:
        col.markdown(
            f'<div class="stat-card">'
            f'<div class="stat-label">{label}</div>'
            f'<div class="stat-value">{value}</div>'
            f'<div class="stat-sub">{sub}</div>'
            f'</div>',
            unsafe_allow_html=True
        )

    st.markdown("<div style='height:.8rem'></div>", unsafe_allow_html=True)

    # ── MoM countdown timers ──────────────────────────────────────────────────
    # Shows a progress bar per past meeting without MoM — red if overdue.
    mom_timers = check_mom_timers(p, meetings)
    if mom_timers:
        st.markdown('<div class="scard" style="margin-bottom:.8rem">', unsafe_allow_html=True)
        st.markdown('<div class="scard-title">⏱️ MoM Timers — Post-Meeting SLA Tracker</div>', unsafe_allow_html=True)
        for m, hrs_left, is_overdue in mom_timers:
            if is_overdue:
                bar_color = "#E53E3E"
                bar_pct   = 100
                badge_cls = "b-red"
                badge_txt = f"OVERDUE {abs(hrs_left):.1f}h"
                icon      = "🔴"
            elif hrs_left < 12:
                bar_color = "#F6A623"
                bar_pct   = max(0, 100 - (hrs_left / 8 * 100))
                badge_cls = "b-orange"
                badge_txt = f"{hrs_left:.1f}h left"
                icon      = "🟠"
            else:
                bar_color = "#38A169"
                bar_pct   = max(0, 100 - (hrs_left / 8 * 100))
                badge_cls = "b-green"
                badge_txt = f"{hrs_left:.1f}h left"
                icon      = "🟢"

            dt           = m.meeting_date.strftime("%b %d, %Y %H:%M")
            deadline_str = m.mom_deadline.strftime("%b %d %H:%M") if m.mom_deadline else "—"
            st.markdown(f"""
            <div style="padding:10px 0;border-bottom:1px solid #F0EFE9">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
                <div style="font-size:13px;font-weight:500;color:#1A1A18">{icon} {m.title[:55]}</div>
                <span class="badge {badge_cls}">{badge_txt}</span>
              </div>
              <div style="font-size:11px;color:#8A8980;margin-bottom:6px">
                Meeting: {dt} &nbsp;·&nbsp; MoM deadline: {deadline_str} &nbsp;·&nbsp; {m.attendees} attendees
              </div>
              <!-- Bar fills left-to-right as time runs out; turns red when overdue -->
              <div style="background:#EEEDE8;border-radius:99px;height:6px;width:100%">
                <div style="height:6px;border-radius:99px;width:{min(bar_pct,100):.0f}%;background:{bar_color}"></div>
              </div>
            </div>""", unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

    # ── Delivery progress + governance rules (left) / Alerts (right) ─────────
    l, r = st.columns([6, 4])
    with l:
        st.markdown('<div class="scard">', unsafe_allow_html=True)
        st.markdown('<div class="scard-title">📈 Delivery Progress</div>', unsafe_allow_html=True)
        st.markdown(prog_bar(p.delivery_pct), unsafe_allow_html=True)
        d1 = p.start_date.strftime("%b %d, %Y")   if p.start_date   else "—"
        d2 = p.go_live_date.strftime("%b %d, %Y") if p.go_live_date else "—"
        st.markdown(
            f'<div style="display:flex;justify-content:space-between;font-size:12px;'
            f'color:#8A8980;margin-top:10px">'
            f'<span>🚀 Start: {d1}</span><span>🏁 Go-Live: {d2}</span></div>',
            unsafe_allow_html=True
        )
        if rules:
            st.markdown('<div style="height:.8rem"></div>', unsafe_allow_html=True)
            st.markdown('<div class="scard-title" style="margin-bottom:.5rem">📋 Governance Rules</div>', unsafe_allow_html=True)
            rows = "".join(
                f'<tr><td>{rule_html(r.rule_type)}</td><td>{r.description[:8]}</td>'
                f'<td style="color:#8A8980">{r.frequency}</td>'
                f'<td style="color:#8A8980">{r.sla_hours}h</td></tr>'
                for r in rules
            )
            st.markdown(
                f'<table class="mini-table"><thead><tr>'
                f'<th>Type</th><th>Description</th><th>Freq</th><th>SLA</th>'
                f'</tr></thead><tbody>{rows}</tbody></table>',
                unsafe_allow_html=True
            )
        st.markdown('</div>', unsafe_allow_html=True)

    with r:
        st.markdown('<div class="scard">', unsafe_allow_html=True)
        st.markdown('<div class="scard-title">🔔 Active Alerts</div>', unsafe_allow_html=True)
        if not alerts:
            st.markdown(
                '<div class="alert-block al-green">'
                '<div class="al-title">All checks passing</div>'
                '<div class="al-desc">No governance issues detected.</div></div>',
                unsafe_allow_html=True
            )
        # Sort alerts: red first, then orange, then green
        for a in sorted(alerts, key=lambda x: {"red": 0, "orange": 1, "green": 2}.get(x.level, 1)):
            cls  = {"red": "al-red", "orange": "al-orange", "green": "al-green"}.get(a.level, "al-orange")
            icon = {"red": "🔴",     "orange": "🟠",         "green": "🟢"}.get(a.level, "🟠")
            st.markdown(
                f'<div class="alert-block {cls}">'
                f'<div class="al-title">{icon} {a.title}</div>'
                f'<div class="al-desc">{a.description}</div></div>',
                unsafe_allow_html=True
            )
        st.markdown('</div>', unsafe_allow_html=True)

    # ── Email category breakdown + recent emails ──────────────────────────────
    el, er = st.columns([4, 6])
    with el:
        st.markdown('<div class="scard">', unsafe_allow_html=True)
        st.markdown('<div class="scard-title">📧 Email Categories</div>', unsafe_allow_html=True)
        cats = {}
        for e in emails:
            cats[e.category] = cats.get(e.category, 0) + 1
        if cats:
            for cat, cnt in sorted(cats.items(), key=lambda x: -x[1]):
                pct = cnt / len(emails) * 100
                st.markdown(
                    f'<div style="display:flex;align-items:center;gap:9px;margin-bottom:9px">'
                    f'{cat_html(cat)}'
                    f'<div style="flex:1"><div class="prog-wrap">'
                    f'<div class="prog-fill" style="width:{pct:.0f}%;background:#5C3FD4"></div>'
                    f'</div></div>'
                    f'<div style="font-size:12px;color:#8A8980">{cnt}</div></div>',
                    unsafe_allow_html=True
                )
        else:
            st.markdown('<div style="font-size:13px;color:#8A8980">No emails synced yet.</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

    with er:
        st.markdown('<div class="scard">', unsafe_allow_html=True)
        st.markdown('<div class="scard-title">📬 Recent Emails</div>', unsafe_allow_html=True)
        if not emails:
            st.markdown(
                '<div style="font-size:13px;color:#8A8980">No emails found. Run sync from terminal.</div>',
                unsafe_allow_html=True
            )
        for e in emails[:6]:
            icon = "⚠️" if e.risk_signal else "📩"
            dt   = e.received_at.strftime("%b %d") if e.received_at else "—"
            # Skip summaries that start with [Gemini — those are stale placeholder values
            summ = e.summary[:90] if e.summary and not e.summary.startswith("[Gemini") else ""
            st.markdown(
                f'<div class="row-item">'
                f'<div class="row-icon" style="background:#F0EFE9">{icon}</div>'
                f'<div style="flex:1">'
                f'<div class="row-title">{e.subject[:55]} &nbsp;{cat_html(e.category)}</div>'
                f'<div class="row-sub">{dt} · {e.sender[:35]}'
                f'{("<br>" + summ) if summ else ""}</div></div></div>',
                unsafe_allow_html=True
            )
        st.markdown('</div>', unsafe_allow_html=True)

    # ── Meetings summary ──────────────────────────────────────────────────────
    st.markdown('<div class="scard">', unsafe_allow_html=True)
    st.markdown('<div class="scard-title">📅 Meetings</div>', unsafe_allow_html=True)
    if not meetings:
        st.markdown('<div style="font-size:13px;color:#8A8980">No meetings synced yet.</div>', unsafe_allow_html=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for m in sorted(meetings, key=lambda x: x.meeting_date, reverse=True)[:6]:
        status = "🔜 Upcoming" if m.meeting_date >= now else "✅ Past"
        mom    = '<span class="badge b-green">MoM ✓</span>' if m.mom_sent else '<span class="badge b-red">MoM ❌</span>'
        dt     = m.meeting_date.strftime("%b %d, %Y %H:%M")
        summ   = m.summary[:80] if m.summary and not m.summary.startswith("[Gemini") else ""
        st.markdown(
            f'<div class="row-item">'
            f'<div class="row-icon" style="background:#F0EFE9">📅</div>'
            f'<div style="flex:1">'
            f'<div class="row-title">{m.title[:55]} &nbsp;{mom}</div>'
            f'<div class="row-sub">{status} · {dt} · {m.attendees} attendees'
            f'{("<br>" + summ) if summ else ""}</div></div></div>',
            unsafe_allow_html=True
        )
    st.markdown('</div>', unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: EMAILS
# ══════════════════════════════════════════════════════════════════════════════

elif page == "Emails":
    if not selected_id:
        st.warning("Select a project first.")
        st.stop()

    p, emails, _, _, _, _ = load(selected_id)
    st.markdown(f'<div class="page-title">📧 Emails — {p.project_id}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="page-sub">{p.name} · {p.client_name}</div>', unsafe_allow_html=True)

    # ── Filter controls ───────────────────────────────────────────────────────
    fc1, fc2, fc3 = st.columns([3, 3, 4])
    with fc1: cat_f  = st.selectbox("Category", ["All", "MoM", "WBR", "Delay", "Escalation", "General"])
    with fc2: risk_f = st.selectbox("Risk",     ["All", "Risk only", "No risk"])
    with fc3: search = st.text_input("Search emails", placeholder="Subject, sender, or summary")

    # Apply filters in sequence
    fil = emails
    if cat_f  != "All":       fil = [e for e in fil if e.category == cat_f]
    if risk_f == "Risk only": fil = [e for e in fil if e.risk_signal]
    elif risk_f == "No risk": fil = [e for e in fil if not e.risk_signal]
    if search:
        needle = search.lower()
        fil = [
            e for e in fil
            if needle in " ".join([e.subject or "", e.sender or "", e.snippet or "", e.summary or ""]).lower()
        ]

    st.markdown(
        f'<div style="font-size:12px;color:#8A8980;margin-bottom:.7rem">'
        f'Showing {len(fil)} of {len(emails)} emails</div>',
        unsafe_allow_html=True
    )

    if not fil:
        st.info("No emails match the filter.")
    else:
        st.markdown('<div class="scard">', unsafe_allow_html=True)
        for e in fil:
            icon    = "⚠️" if e.risk_signal else "📩"
            dt      = e.received_at.strftime("%b %d, %Y") if e.received_at else "—"
            summ    = e.summary[:110] if e.summary and not e.summary.startswith("[Gemini") else "—"
            link    = gmail_thread_url(getattr(e, "gmail_thread_id", "") or e.gmail_msg_id)
            thread_html = f' · <a href="{link}" target="_blank">Open thread</a>' if link else ""
            st.markdown(
                f'<div class="row-item">'
                f'<div class="row-icon" style="background:#F0EFE9">{icon}</div>'
                f'<div style="flex:1">'
                f'<div class="row-title">{e.subject[:70]} &nbsp;{cat_html(e.category)}</div>'
                f'<div class="row-sub">{dt} · {e.sender[:50]}{thread_html}<br>{summ}</div>'
                f'</div></div>',
                unsafe_allow_html=True
            )
        st.markdown('</div>', unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: ATTACH EMAILS
# ══════════════════════════════════════════════════════════════════════════════

elif page == "Attach Emails":
    if not selected_id:
        st.warning("Select a project first.")
        st.stop()

    s                = db()
    all_projects     = s.query(Project).order_by(Project.project_id).all()
    selected_project = s.query(Project).filter_by(project_id=selected_id).first()
    # Load only unassigned unmapped emails — sorted newest first
    unassigned = (
        s.query(UnmappedEmail)
        .filter(UnmappedEmail.assigned_project_id.is_(None))
        .order_by(UnmappedEmail.received_at.desc())
        .all()
    )
    s.close()

    st.markdown(
        f'<div class="page-title">🔗 Attach Email Threads — {selected_project.project_id}</div>',
        unsafe_allow_html=True
    )
    st.markdown(
        '<div class="page-sub">Review unmapped Gmail threads, open the original conversation, '
        'and attach the right ones to a project.</div>',
        unsafe_allow_html=True
    )

    # ── Target project selector ───────────────────────────────────────────────
    target_ids        = [p.project_id for p in all_projects]
    default_idx       = target_ids.index(selected_id) if selected_id in target_ids else 0
    target_project_id = st.selectbox("Attach to project", target_ids, index=default_idx)

    # Option to attach all emails from the same Gmail thread at once
    bulk_thread_attach = st.checkbox(
        "Attach all unassigned emails in the same Gmail thread",
        value = False,
        help  = "Use only when the thread discusses one project. Leave off for mixed-project threads.",
    )
    search_unmapped = st.text_input("Filter unmapped emails", placeholder="Subject, sender, or snippet")
    if search_unmapped:
        needle     = search_unmapped.lower()
        unassigned = [
            u for u in unassigned
            if needle in " ".join([u.subject or "", u.sender or "", u.snippet or "", u.body_preview or ""]).lower()
        ]

    st.markdown(
        f'<div style="font-size:12px;color:#8A8980;margin-bottom:.7rem">'
        f'{len(unassigned)} unassigned email thread(s)</div>',
        unsafe_allow_html=True
    )

    if not unassigned:
        st.info("No unmapped email threads waiting for manual attachment.")
    else:
        st.markdown('<div class="scard">', unsafe_allow_html=True)
        for u in unassigned:
            dt        = u.received_at.strftime("%b %d, %Y") if u.received_at else "—"
            thread_id = getattr(u, "gmail_thread_id", "") or u.gmail_msg_id
            link      = gmail_thread_url(thread_id)
            body      = (u.body_preview or u.snippet or "")[:180]

            st.markdown(
                f'<div class="row-item">'
                f'<div class="row-icon" style="background:#F0EFE9">📩</div>'
                f'<div style="flex:1">'
                f'<div class="row-title">{(u.subject or "(no subject)")[:90]}</div>'
                f'<div class="row-sub">{dt} · {(u.sender or "")[:60]} · '
                f'<a href="{link}" target="_blank">Open thread</a><br>{body}</div>'
                f'</div></div>',
                unsafe_allow_html=True,
            )

            if st.button(f"Attach to {target_project_id}", key=f"attach_unmapped_{u.id}", type="secondary"):
                try:
                    s          = db()
                    target     = s.query(Project).filter_by(project_id=target_project_id).first()
                    unmapped   = s.query(UnmappedEmail).filter_by(id=u.id).first()

                    if not target or not unmapped:
                        st.error("Could not find the selected project or email.")
                        continue

                    thread_id    = getattr(unmapped, "gmail_thread_id", "") or unmapped.gmail_msg_id
                    same_thread  = [unmapped]

                    if bulk_thread_attach:
                        # Find all unassigned emails in the same Gmail thread
                        same_thread = (
                            s.query(UnmappedEmail).filter(
                                UnmappedEmail.assigned_project_id.is_(None),
                                UnmappedEmail.gmail_thread_id == thread_id,
                            ).all() or [unmapped]
                        )

                    attached_count = 0
                    for pending in same_thread:
                        if attach_unmapped_to_project(s, pending, target):
                            attached_count += 1

                    s.commit()
                    st.success(f"Attached {attached_count or len(same_thread)} thread email(s) to {target_project_id}.")
                    st.rerun()

                except Exception as ex:
                    st.error(f"Attach failed: {ex}")
                finally:
                    try:
                        s.close()
                    except Exception:
                        pass

        st.markdown('</div>', unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: MEETINGS
# ══════════════════════════════════════════════════════════════════════════════

elif page == "Meetings":
    if not selected_id:
        st.warning("Select a project first.")
        st.stop()

    p, _, meetings, _, _, _ = load(selected_id)
    st.markdown(f'<div class="page-title">📅 Meetings — {p.project_id}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="page-sub">{p.name} · {p.client_name}</div>', unsafe_allow_html=True)

    now      = datetime.now(timezone.utc).replace(tzinfo=None)
    upcoming = sorted([m for m in meetings if m.meeting_date >= now], key=lambda x: x.meeting_date)
    past     = sorted([m for m in meetings if m.meeting_date < now],  key=lambda x: x.meeting_date, reverse=True)

    if not meetings:
        st.info("No meetings found. Run `python main.py sync` from terminal.")

    # ── Upcoming meetings ─────────────────────────────────────────────────────
    if upcoming:
        st.markdown(
            '<div style="font-size:12px;font-weight:600;color:#8A8980;text-transform:uppercase;'
            'letter-spacing:.06em;margin-bottom:.5rem">Upcoming</div>',
            unsafe_allow_html=True
        )
        st.markdown('<div class="scard">', unsafe_allow_html=True)
        for m in upcoming:
            dt   = m.meeting_date.strftime("%b %d, %Y  %H:%M")
            mom  = '<span class="badge b-green">MoM ✓</span>' if m.mom_sent else '<span class="badge b-orange">MoM missing</span>'
            summ = m.summary[:100] if m.summary and not m.summary.startswith("[Gemini") else ""
            st.markdown(
                f'<div class="row-item">'
                f'<div class="row-icon" style="background:#EAE7FC;color:#3D2D9E">🔜</div>'
                f'<div style="flex:1">'
                f'<div class="row-title">{m.title} &nbsp;{mom}</div>'
                f'<div class="row-sub">{dt} · {m.attendees} attendees'
                f'{("<br>" + summ) if summ else ""}</div></div></div>',
                unsafe_allow_html=True
            )
        st.markdown('</div>', unsafe_allow_html=True)

    # ── Past meetings ─────────────────────────────────────────────────────────
    if past:
        st.markdown(
            '<div style="font-size:12px;font-weight:600;color:#8A8980;text-transform:uppercase;'
            'letter-spacing:.06em;margin:.8rem 0 .5rem">Past Meetings</div>',
            unsafe_allow_html=True
        )
        st.markdown('<div class="scard">', unsafe_allow_html=True)
        for m in past:
            dt   = m.meeting_date.strftime("%b %d, %Y  %H:%M")
            mom  = '<span class="badge b-green">MoM ✓</span>' if m.mom_sent else '<span class="badge b-red">MoM ❌</span>'
            summ = m.summary[:100] if m.summary and not m.summary.startswith("[Gemini") else ""
            st.markdown(
                f'<div class="row-item">'
                f'<div class="row-icon" style="background:#F0EFE9">📅</div>'
                f'<div style="flex:1">'
                f'<div class="row-title">{m.title} &nbsp;{mom}</div>'
                f'<div class="row-sub">{dt} · {m.attendees} attendees'
                f'{("<br>" + summ) if summ else ""}</div></div></div>',
                unsafe_allow_html=True
            )
        st.markdown('</div>', unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: ALERTS
# ══════════════════════════════════════════════════════════════════════════════

elif page == "Alerts":
    if not selected_id:
        st.warning("Select a project first.")
        st.stop()

    p, emails, meetings, alerts, _, rules = load(selected_id)
    st.markdown(f'<div class="page-title">🔔 Governance Alerts — {p.project_id}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="page-sub">Health: {health_html(p.health)}</div>', unsafe_allow_html=True)

    # Manual trigger button — runs the full governance check immediately
    if st.button("🔄  Run governance check now", type="primary"):
        with st.spinner("Analysing emails, meetings, and rules…"):
            try:
                run_alerts(p, emails, meetings, rules)
                st.success("Done!")
                st.rerun()
            except Exception as ex:
                st.error(f"Error: {ex}")

    st.markdown("<div style='height:.5rem'></div>", unsafe_allow_html=True)

    if not alerts:
        st.markdown(
            '<div class="alert-block al-green">'
            '<div class="al-title">✅ All checks passing</div>'
            '<div class="al-desc">No active governance issues detected.</div></div>',
            unsafe_allow_html=True
        )

    # Sort: red first, then orange, then green
    for a in sorted(alerts, key=lambda x: {"red": 0, "orange": 1, "green": 2}.get(x.level, 1)):
        cls  = {"red": "al-red", "orange": "al-orange", "green": "al-green"}.get(a.level, "al-orange")
        icon = {"red": "🔴",     "orange": "🟠",         "green": "🟢"}.get(a.level, "🟠")
        ts   = a.created_at.strftime("%b %d, %Y  %H:%M") if a.created_at else ""
        st.markdown(
            f'<div class="alert-block {cls}">'
            f'<div class="al-title">{icon} {a.title}</div>'
            f'<div class="al-desc">{a.description}</div>'
            f'<div class="al-time">{ts}</div></div>',
            unsafe_allow_html=True
        )


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: MEMBERS
# ══════════════════════════════════════════════════════════════════════════════

elif page == "Members":
    if not selected_id:
        st.warning("Select a project first.")
        st.stop()
    if not HAS_MEMBERS:
        st.warning("Replace models.py with the updated version to enable Members.")
        st.stop()

    p, _, _, _, members, _ = load(selected_id)
    st.markdown(f'<div class="page-title">👥 Team Members — {p.project_id}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="page-sub">{p.name} · {len(members)} member(s)</div>', unsafe_allow_html=True)

    if not members:
        st.info("No members yet. Use Add Member or run `python main.py sync`.")
    else:
        # Group members by role for cleaner display
        for role in sorted(set(m.role for m in members)):
            rm = [m for m in members if m.role == role]
            st.markdown(
                f'<div style="font-size:11px;font-weight:600;color:#8A8980;text-transform:uppercase;'
                f'letter-spacing:.06em;margin:.9rem 0 .4rem">{role} ({len(rm)})</div>',
                unsafe_allow_html=True
            )
            st.markdown('<div class="scard" style="padding:.8rem 1.2rem">', unsafe_allow_html=True)
            for m in rm:
                # Show source badge: teal for auto-extracted, purple for manually added
                src = (
                    '<span class="badge b-teal">email</span>'
                    if m.source == "email"
                    else '<span class="badge b-purple">manual</span>'
                )
                st.markdown(
                    f'<div class="row-item">'
                    f'<div class="avatar">{initials(m.name)}</div>'
                    f'<div style="flex:1">'
                    f'<div class="row-title">{m.name} &nbsp;{src}</div>'
                    f'<div class="row-sub">{m.email}</div>'
                    f'</div></div>',
                    unsafe_allow_html=True
                )
            st.markdown('</div>', unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: ADD PROJECT
# ══════════════════════════════════════════════════════════════════════════════

elif page == "Add Project":
    st.markdown('<div class="page-title">➕ Add New Project</div>', unsafe_allow_html=True)
    s_preview        = db()
    next_project_id  = generate_project_id(s_preview)
    s_preview.close()
    st.markdown(
        f'<div class="page-sub">System project ID will be <b>{next_project_id}</b>.</div>',
        unsafe_allow_html=True
    )

    st.markdown('<div class="scard">', unsafe_allow_html=True)
    with st.form("add_project"):
        c1, c2 = st.columns(2)
        with c1:
            name          = st.text_input("Project Name *",  placeholder="Retail ERP Migration")
            client_name   = st.text_input("Client Name *",   placeholder="RetailMax India Pvt Ltd")
            delivery_lead = st.text_input("Delivery Lead",   placeholder="Priya Sharma")
            pm_email      = st.text_input(
                "PM Email *",
                placeholder="pm@oneture.com",
                help="Confirmation email will be sent here.",
            )
            engagement = st.text_input("Engagement", placeholder="Fixed-Price 18 months")
        with c2:
            description      = st.text_area("Description",  placeholder="Brief project description", height=90)
            start_date       = st.date_input("Start Date")
            go_live_date     = st.date_input("Go-Live Date")
            delivery_pct     = st.slider("Delivery % done", 0, 100, 0)
            gmail_query      = st.text_input("Gmail Query",      placeholder=f"subject:{next_project_id}")
            calendar_keyword = st.text_input("Calendar Keyword", placeholder="RetailMax")

        if st.form_submit_button("💾  Save Project", type="primary"):
            if not name or not client_name:
                st.error("Project name and client are required.")
            elif not pm_email or "@" not in pm_email:
                st.error("A valid PM Email is required to send the confirmation.")
            else:
                try:
                    s          = db()
                    project_id = generate_project_id(s)

                    if s.query(Project).filter_by(project_id=project_id).first():
                        st.error(f"Project {project_id} already exists.")
                        s.close()
                    else:
                        new_proj = Project(
                            project_id       = project_id,
                            name             = name,
                            client_name      = client_name,
                            description      = description,
                            delivery_lead    = delivery_lead,
                            engagement       = engagement,
                            delivery_pct     = delivery_pct,
                            gmail_query      = gmail_query,
                            calendar_keyword = calendar_keyword,
                            pm_email         = pm_email,
                            pm_confirmed     = False,
                            # Combine date picker value with midnight time to get datetime
                            start_date       = datetime.combine(start_date,   datetime.min.time()),
                            go_live_date     = datetime.combine(go_live_date, datetime.min.time()),
                        )
                        s.add(new_proj)
                        s.flush()   # get project.id before inserting rules

                        create_default_rules(s, new_proj)
                        s.commit()

                        # Generate magic link token and send confirmation email to PM
                        import secrets as _secrets
                        saved = s.query(Project).filter_by(project_id=project_id).first()
                        saved.pm_confirm_token = _secrets.token_urlsafe(32)
                        s.commit()

                        try:
                            from govtrack.services.Notifier import send_pm_confirmation_request
                            send_pm_confirmation_request(saved, pm_email, member_emails=[])
                            st.success(f"✅ Project {project_id} — {name} created! Confirmation email sent to {pm_email}")
                        except Exception as mail_err:
                            st.success(f"✅ Project {project_id} — {name} created!")
                            st.warning(f"⚠️ Could not send confirmation email: {mail_err}")

                        s.close()
                        st.rerun()

                except Exception as ex:
                    st.error(f"Error: {ex}")

    st.markdown('</div>', unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: ADD MEMBER
# ══════════════════════════════════════════════════════════════════════════════

elif page == "Add Member":
    if not selected_id:
        st.warning("Select a project first.")
        st.stop()
    if not HAS_MEMBERS:
        st.warning("Replace models.py with the updated version to enable Members.")
        st.stop()

    p, _, _, _, _, _ = load(selected_id)
    st.markdown(f'<div class="page-title">👤 Add Member — {p.project_id}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="page-sub">Add a team member to {p.name}</div>', unsafe_allow_html=True)

    st.markdown('<div class="scard">', unsafe_allow_html=True)
    with st.form("add_member"):
        name  = st.text_input("Full Name *", placeholder="Priya Sharma")
        email = st.text_input("Email *",     placeholder="priya@example.com")
        role  = st.selectbox("Role", [
            "Delivery Lead", "Developer", "Business Analyst", "QA Engineer",
            "Project Manager", "Client", "Scrum Master", "Architect", "Team Member"
        ])

        if st.form_submit_button("➕  Add Member", type="primary"):
            if not name or not email:
                st.error("Name and Email are required.")
            else:
                try:
                    s        = db()
                    proj     = s.query(Project).filter_by(project_id=selected_id).first()
                    existing = s.query(Member).filter_by(project_id=proj.id, email=email).first()

                    if existing:
                        # Update instead of duplicating
                        existing.name = name
                        existing.role = role
                        s.commit()
                        st.success(f"Updated {name}")
                    else:
                        s.add(Member(
                            project_id = proj.id,
                            name       = name,
                            email      = email,
                            role       = role,
                            source     = "manual",
                        ))
                        s.commit()
                        st.success(f"✅ {name} added as {role}")

                    s.close()
                    st.rerun()
                except Exception as ex:
                    st.error(f"Error: {ex}")

    st.markdown('</div>', unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: IMPORT PDF
# ══════════════════════════════════════════════════════════════════════════════

elif page == "Import PDF":
    st.markdown('<div class="page-title">📄 Import from PDF</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="page-sub">Upload a project brief PDF — GovTrack extracts all details automatically.</div>',
        unsafe_allow_html=True
    )

    uploaded = st.file_uploader("Choose a PDF file", type=["pdf"])
    if uploaded:
        # Write uploaded file to a temp path so extract_from_pdf() can read it
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(uploaded.read())
            tmp_path = tmp.name

        try:
            from govtrack.integrations.import_pdf import extract_from_pdf
            with st.spinner("Reading PDF…"):
                data = extract_from_pdf(tmp_path)

            st.success("PDF read successfully!")
            st.markdown('<div class="scard">', unsafe_allow_html=True)
            st.markdown('<div class="scard-title">Extracted Details</div>', unsafe_allow_html=True)

            c1, c2 = st.columns(2)
            with c1:
                for k, v in data.items():
                    # Skip governance_rules (shown separately) and project_id (auto-generated)
                    if k not in ("governance_rules", "project_id") and v:
                        st.markdown(
                            f'<div style="display:flex;gap:8px;margin-bottom:5px">'
                            f'<span style="font-size:12px;color:#8A8980;min-width:140px">{k}</span>'
                            f'<span style="font-size:13px;color:#1A1A18">{v}</span></div>',
                            unsafe_allow_html=True
                        )
            with c2:
                st.markdown(
                    f'<div style="font-size:13px;font-weight:500;margin-bottom:.5rem">'
                    f'Governance Rules ({len(data.get("governance_rules", []))})</div>',
                    unsafe_allow_html=True
                )
                for r in data.get("governance_rules", []):
                    st.markdown(
                        f'<div class="row-item" style="padding:6px 0">'
                        f'<div style="flex:1">'
                        f'<div class="row-title">{rule_html(r["rule_type"])} &nbsp;{r["description"][:50]}</div>'
                        f'<div class="row-sub">{r["frequency"]} · SLA {r["sla_hours"]}h</div>'
                        f'</div></div>',
                        unsafe_allow_html=True
                    )
            st.markdown('</div>', unsafe_allow_html=True)

            # PM email required before saving — confirmation email will be sent here
            pdf_pm_email = st.text_input(
                "PM Email *",
                placeholder="pm@oneture.com",
                help="A confirmation email will be sent to this address once the project is saved.",
                key="pdf_pm_email",
            )

            if st.button("💾  Save to database", type="primary"):
                if not pdf_pm_email or "@" not in pdf_pm_email:
                    st.error("Please enter a valid PM Email before saving.")
                else:
                    try:
                        s                    = db()
                        generated_project_id = generate_project_id(s)

                        # Note: existing=None always here — upsert logic is a future enhancement
                        existing = None
                        if existing:
                            for f in ["name", "client_name", "description", "delivery_lead",
                                      "engagement", "delivery_pct", "gmail_query", "calendar_keyword"]:
                                if data.get(f) is not None:
                                    setattr(existing, f, data[f])
                            if data.get("start_date"):
                                existing.start_date = datetime.strptime(data["start_date"], "%Y-%m-%d")
                            if data.get("go_live_date"):
                                existing.go_live_date = datetime.strptime(data["go_live_date"], "%Y-%m-%d")
                            existing.pm_email = pdf_pm_email
                            proj = existing
                        else:
                            proj = Project(
                                project_id       = generated_project_id,
                                name             = data.get("name")             or "Unnamed",
                                client_name      = data.get("client_name")      or "",
                                description      = data.get("description")      or "",
                                delivery_lead    = data.get("delivery_lead")    or "",
                                engagement       = data.get("engagement")       or "",
                                delivery_pct     = float(data.get("delivery_pct") or 0),
                                gmail_query      = data.get("gmail_query")      or f"subject:{generated_project_id}",
                                calendar_keyword = data.get("calendar_keyword") or "",
                                pm_email         = pdf_pm_email,
                                pm_confirmed     = False,
                                start_date       = datetime.strptime(data["start_date"],   "%Y-%m-%d") if data.get("start_date")   else None,
                                go_live_date     = datetime.strptime(data["go_live_date"], "%Y-%m-%d") if data.get("go_live_date") else None,
                            )
                            s.add(proj)
                            s.flush()   # get project.id before inserting rules

                        # Replace governance rules — skip any non-standard types
                        s.query(GovernanceRule).filter_by(project_id=proj.id).delete()
                        for r in data.get("governance_rules", []):
                            if r.get("rule_type") not in ("MOM_SLA", "WBR_SLA"):
                                continue
                            s.add(GovernanceRule(
                                project_id  = proj.id,
                                rule_type   = r.get("rule_type",   "MOM_SLA"),
                                description = r.get("description", ""),
                                sla_hours   = int(r.get("sla_hours", 0)),
                                frequency   = r.get("frequency",   "weekly"),
                            ))
                        s.commit()

                        # Re-query to get the saved object with its DB-assigned id
                        saved_proj = s.query(Project).filter_by(project_id=proj.project_id).first()
                        pid        = saved_proj.project_id
                        pname      = saved_proj.name

                        # Generate and save the magic link token, then send confirmation
                        import secrets as _secrets
                        saved_proj.pm_confirm_token = _secrets.token_urlsafe(32)
                        s.commit()

                        try:
                            from govtrack.services.Notifier import send_pm_confirmation_request
                            send_pm_confirmation_request(saved_proj, pdf_pm_email, member_emails=[])
                            st.success(f"✅ {pid} — {pname} saved! Confirmation email sent to {pdf_pm_email}")
                        except Exception as mail_err:
                            st.success(f"✅ {pid} — {pname} saved!")
                            st.warning(f"⚠️ Could not send confirmation email: {mail_err}")

                        s.close()
                        st.rerun()

                    except Exception as ex:
                        st.error(f"Save error: {ex}")

        except Exception as ex:
            st.error(f"Could not read PDF: {ex}")
        finally:
            # Always delete the temp file even if an error occurred
            try:
                os.unlink(tmp_path)
            except Exception:
                pass