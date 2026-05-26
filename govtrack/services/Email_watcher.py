"""
Email_watcher.py — Real-time inbox watcher for GovTrack.

Runs as a background thread, polling Gmail every 5 seconds.
For each new Oneture-involved email it:
  1. Tries to match it to an existing project (6-layer engine)
  2. If matched  → saves email + syncs emails/meetings
  3. If unmatched → runs AI multi-project extraction
  4. If AI finds projects → auto-creates them
  5. If AI finds nothing  → saves to unmapped_emails + alerts manager

Threading:
  start_watcher()   → starts the background thread
  stop_watcher()    → signals the thread to stop
  watcher_running() → checks if thread is alive
"""

import re
import time
import base64
import secrets
import threading
import json
import os
from email.utils import getaddresses
from datetime import datetime

import requests

from govtrack.core.models import (
    Session, Project, GovernanceRule,
    Member, Email, UnmappedEmail,
)
from govtrack.core.google_auth import gmail_service
from govtrack.integrations.gmail_reader import fetch_emails
from govtrack.integrations.calendar_reader import fetch_meetings
from govtrack.ai.gemini import classify_email, summarize_email

# Load .env — wrapped in try/except so watcher still starts
# if paths module is unavailable (e.g. during testing)
try:
    from dotenv import load_dotenv
    from govtrack.core.paths import ENV_PATH
    load_dotenv(ENV_PATH)
except Exception:
    pass

# Notifier import — both paths are the same module;
# kept for resilience across different import contexts
try:
    from govtrack.services.Notifier import send_pm_confirmation_request
except ImportError:
    from govtrack.services.Notifier import send_pm_confirmation_request


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

# How often the watcher polls Gmail for new messages
POLL_INTERVAL_SECONDS = 1

# Gmail label applied to every processed message to prevent reprocessing
GOVTRACK_LABEL = "GovTrack-Processed"

# Only fetch inbox emails not yet processed, from the last 14 days
WATCHER_QUERY = f"in:inbox -label:{GOVTRACK_LABEL} newer_than:14d"

# Matches explicit project IDs like PRJ-1001 or PRJ-123456 anywhere in text
PROJECT_ID_PATTERN = re.compile(r"\bPRJ-\d{3,6}\b", re.IGNORECASE)

# Gemini API key — optional. Used for AI matching and multi-project extraction.
# If not set, those features are skipped and rule-based matching is used only.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Minimum confidence score (0–100) for AI project matching to be accepted
AI_MATCH_CONFIDENCE_THRESHOLD = 75

# Only emails involving at least one of these domains are processed.
# Typo variant "onerute.com" included intentionally for resilience.
ONETURE_EMAIL_DOMAINS = ("@oneture.com",)

# Keywords that signal an email is about project/delivery work.
# Used to decide whether an unmatched email is worth running AI extraction on.
PROJECT_SIGNAL_KEYWORDS = [
    "project", "engagement", "kickoff", "kick-off",
    "delivery", "milestone", "migration", "implementation", "sprint",
    "proposal", "contract", "scope", "requirement", "go-live",
    "integration", "platform", "product", "development", "launch",
]

# Default governance rules applied to every auto-created project
DEFAULT_RULES = [
    {
        "rule_type":   "MOM_SLA",
        "description": "Minutes of Meeting must be sent after every meeting",
        "sla_hours":   8,
        "frequency":   "per_meeting",
    },
    {
        "rule_type":   "WBR_SLA",
        "description": "Weekly Business Review report",
        "sla_hours":   24,
        "frequency":   "weekly",
    },
]

# Thread control — module-level so start/stop/status functions share state
_watcher_thread = None
_stop_event     = threading.Event()

# Personal email providers whose domains are not useful as client names
GENERIC_EMAIL_PROVIDERS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "icloud.com", "protonmail.com", "me.com", "live.com",
    "rediffmail.com", "ymail.com",
}

NOISE_SENDER_DOMAINS = {
    "buddy4study.com", "unstop.com", "dare2compete.com", "groww.in",
    "reddit.com", "naukri.com", "foundit.in", "shine.com",
    "internshala.com", "youtube.com", "linkedin.com",
    "facebookmail.com", "zomato.com", "mailers.zomato.com",
    "send.vidiq.com", "googlemail.com",
}

NOISE_SUBJECT_PATTERNS = [
    r"unsubscribe", r"newsletter", r"internship.*alert", r"contest.*alert",
    r"job alert", r"hiring", r"boost rate", r"memorial day",
    r"shared a video", r"got 1 minute", r"delivery status notification",
    r"mailer-daemon", r"zero to one series", r"advisory session",
]


# ══════════════════════════════════════════════════════════════════════════════
# PROJECT ID GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def _generate_project_id(db) -> str:
    """
    Generate the next sequential project ID in PRJ-XXXX format.
    Scans all existing IDs, finds the highest number, and increments it.
    Minimum is PRJ-1000 (starts at 999 + 1) to keep IDs 4 digits minimum.
    """
    existing = db.query(Project.project_id).all()
    max_num  = 999
    for (pid,) in existing:
        m = re.match(r"PRJ-(\d+)", (pid or "").upper())
        if m:
            max_num = max(max_num, int(m.group(1)))
    return f"PRJ-{max_num + 1:04d}"


# ══════════════════════════════════════════════════════════════════════════════
# GMAIL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _get_or_create_label(service) -> str:
    """
    Get or create the 'GovTrack-Processed' Gmail label.
    Applied to every processed message so the watcher never re-processes it.
    Returns the label ID needed for modify() calls.
    """
    labels = service.users().labels().list(userId="me").execute()
    for lbl in labels.get("labels", []):
        if lbl["name"] == GOVTRACK_LABEL:
            return lbl["id"]
    # Label doesn't exist yet — create it
    created = service.users().labels().create(
        userId="me",
        body={
            "name":                  GOVTRACK_LABEL,
            "labelListVisibility":   "labelShow",
            "messageListVisibility": "show",
        },
    ).execute()
    return created["id"]


def _mark_processed(service, msg_id: str, label_id: str):
    """
    Mark a message as fully processed:
    - Adds GovTrack-Processed label (prevents reprocessing)
    - Removes UNREAD label (cleans up inbox)
    """
    service.users().messages().modify(
        userId="me", id=msg_id,
        body={"addLabelIds": [label_id], "removeLabelIds": ["UNREAD"]},
    ).execute()


def _mark_seen_by_govtrack(service, msg_id: str, label_id: str):
    """
    Mark a message as seen (label only — no UNREAD removal).
    Used for emails that were intentionally skipped (non-Oneture, no signals)
    so the watcher doesn't keep re-evaluating them every poll cycle.
    """
    service.users().messages().modify(
        userId="me", id=msg_id,
        body={"addLabelIds": [label_id]},
    ).execute()


def _extract_body_from_payload(payload: dict) -> str:
    """
    Recursively extract plain-text body from a Gmail message payload.
    Gmail nests body data inside 'parts' for multipart messages.
    Returns empty string if no plain-text part is found.
    """
    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(
            payload["body"]["data"]
        ).decode("utf-8", errors="ignore")

    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(
                part["body"]["data"]
            ).decode("utf-8", errors="ignore")
        # Recurse into nested parts (e.g. multipart/alternative inside multipart/mixed)
        nested = _extract_body_from_payload(part)
        if nested:
            return nested
    return ""


def _get_full_email(service, msg_id: str) -> dict:
    """
    Fetch a complete Gmail message and return a structured dict.

    Parses sender name/email from the From header (handles both
    "Display Name <email>" and bare "email" formats).

    Returns keys: subject, sender_name, sender_email, to_members,
                  cc_members, snippet, body, thread_id, received_at
    """
    detail  = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}

    subject     = headers.get("Subject", "")
    from_raw    = headers.get("From", "")
    to_raw      = headers.get("To", "")
    cc_raw      = headers.get("Cc", "")
    snippet     = detail.get("snippet", "")
    thread_id   = detail.get("threadId", "")

    # Convert Gmail's internalDate (milliseconds) to UTC datetime
    received_at = (
        datetime.utcfromtimestamp(int(detail.get("internalDate", 0)) / 1000)
        if detail.get("internalDate") else datetime.utcnow()
    )

    # Parse "Display Name <email@domain.com>" format
    m = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>$', from_raw.strip())
    if m:
        sender_name  = m.group(1).strip()
        sender_email = m.group(2).strip().lower()
    else:
        # Bare email address — derive display name from local part
        sender_email = from_raw.strip().lower()
        sender_name  = sender_email.split("@")[0]

    body = _extract_body_from_payload(detail.get("payload", {}))

    return {
        "subject":      subject,
        "sender_name":  sender_name,
        "sender_email": sender_email,
        "to_members":   _parse_recipients(to_raw),
        "cc_members":   _parse_recipients(cc_raw),
        "snippet":      snippet,
        "body":         body,
        "thread_id":    thread_id,
        "received_at":  received_at,
    }


def _parse_recipients(raw_value: str) -> list:
    """
    Parse a To:/Cc: header into a list of {name, email} dicts.
    Uses email.utils.getaddresses to handle comma-separated addresses correctly.
    Derives display name from local part if no name is provided.
    """
    members = []
    for name, email in getaddresses([raw_value or ""]):
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            continue
        clean_name = (name or "").strip().strip('"')
        if not clean_name:
            clean_name = email.split("@")[0].replace(".", " ").title()
        members.append({"name": clean_name, "email": email})
    return members


# ══════════════════════════════════════════════════════════════════════════════
# CLIENT IDENTIFICATION
# ══════════════════════════════════════════════════════════════════════════════

def _identify_client_from_email(email_data: dict) -> str:
    """
    Identify the client company name from an email using 4 strategies in order:

      1. Non-Oneture CC recipient domain  → most reliable (they ARE the client)
      2. External sender domain           → sender is from client company
      3. Non-Oneture To recipient domain  → client is in the To field
      4. Explicit "Client:" label in body → PM wrote the client name explicitly

    Returns company name string, or "" if identification fails.
    Generic providers (gmail.com, etc.) are always skipped.
    """
    def _domain_to_company(email_addr: str) -> str:
        """Convert an email address to a company name via its domain."""
        if not email_addr or "@" not in email_addr:
            return ""
        domain = email_addr.split("@", 1)[1].lower()
        # Skip Oneture's own domains
        if any(email_addr.endswith(d) for d in ONETURE_EMAIL_DOMAINS):
            return ""
        # Skip personal/generic providers — not useful as client names
        if domain in GENERIC_EMAIL_PROVIDERS:
            return ""
        company = domain.split(".")[0]
        return company.replace("-", " ").title()

    # Strategy 1 — CC recipients are almost always the client side
    for cc in email_data.get("cc_members", []):
        company = _domain_to_company(cc["email"])
        if company:
            return company

    # Strategy 2 — external sender
    company = _domain_to_company(email_data.get("sender_email", ""))
    if company:
        return company

    # Strategy 3 — To recipients
    for to in email_data.get("to_members", []):
        company = _domain_to_company(to["email"])
        if company:
            return company

    # Strategy 4 — explicit body label
    body_text = f"{email_data.get('body', '')} {email_data.get('snippet', '')}"
    for label in ["Client Name", "Client"]:
        m = re.search(re.escape(label) + r"\s*[:\-]\s*(.+)", body_text, re.IGNORECASE)
        if m:
            val = m.group(1).strip().split("\n")[0][:100]
            if val:
                return val

    return ""


def _identify_pm_from_email(email_data: dict) -> tuple:
    """
    Identify the Project Manager from an email.

    Logic:
      - If sender is @oneture.com → they are the PM
      - Otherwise → look for an @oneture.com person in CC/To

    Returns (pm_name: str, pm_email: str).
    Falls back to sender if no Oneture person is found in CC/To.
    """
    sender_email = (email_data.get("sender_email") or "").lower()
    sender_name  = email_data.get("sender_name", "")

    # Sender is Oneture employee → they are the PM
    if any(sender_email.endswith(d) for d in ONETURE_EMAIL_DOMAINS):
        return sender_name, sender_email

    # Look for an Oneture person in CC or To
    for recipient in email_data.get("cc_members", []) + email_data.get("to_members", []):
        if any(recipient["email"].endswith(d) for d in ONETURE_EMAIL_DOMAINS):
            return recipient["name"], recipient["email"]

    # Fall back to sender if no Oneture address found
    return sender_name, sender_email


# ══════════════════════════════════════════════════════════════════════════════
# AI MULTI-PROJECT EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

def _ai_extract_multiple_projects(email_data: dict) -> list:
    """
    Use Gemini to extract ALL project discussions from a single email.
    One email can mention multiple projects — this identifies each one.

    Called when no existing project matched AND the email has work signals.
    Requires GEMINI_API_KEY — returns [] if key is not set.

    Returns a list of project dicts with keys:
      name, client_name, description, pm_name, pm_email,
      start_date, go_live_date, engagement

    Returns [] if no projects found or API call fails.
    """
    if not GEMINI_API_KEY:
        return []

    subject      = email_data.get("subject", "") or ""
    body         = email_data.get("body", "") or ""
    snippet      = email_data.get("snippet", "") or ""
    sender_name  = email_data.get("sender_name", "")
    sender_email = email_data.get("sender_email", "")
    cc_list      = [f"{m['name']} <{m['email']}>" for m in email_data.get("cc_members", [])]
    to_list      = [f"{m['name']} <{m['email']}>" for m in email_data.get("to_members", [])]

    prompt = f"""You are GovTrack, a project governance AI for Oneture Technologies.

Analyze this email and extract ALL project discussions it contains.
One email can discuss MULTIPLE projects — identify each one separately.

EMAIL DETAILS:
Subject: {subject or "(no subject)"}
From: {sender_name} <{sender_email}>
To: {', '.join(to_list) or '(none)'}
CC: {', '.join(cc_list) or '(none)'}
Body:
{(body or snippet)[:3000]}

EXTRACTION RULES:
1. Look for any project or engagement mentioned — explicit or implied
2. client_name: Identify from CC domain (non-Oneture), body mentions of company, or "Client:" label.
   Never use "Unknown Client" — make a best guess from context.
3. pm_name / pm_email: The Oneture person managing the project (sender if @oneture.com, else find in CC/To)
4. name: A concise descriptive project name (NOT "Project PRJ-XXX") — derive from subject/body context
5. description: Brief 1-2 line description of what the project involves
6. start_date / go_live_date: Extract if mentioned, in YYYY-MM-DD format or null
7. engagement: Type of engagement (e.g., "Implementation", "Migration", "Consulting", etc.)
8. If the email clearly has no project/work discussion (promotional, personal, noise), return empty list

Return ONLY valid JSON array (no markdown fences, no explanation):
[
  {{
    "name": "...",
    "client_name": "...",
    "description": "...",
    "pm_name": "...",
    "pm_email": "...",
    "start_date": null,
    "go_live_date": null,
    "engagement": "..."
  }}
]

If no projects found: []
"""

    try:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
        )
        resp = requests.post(
            url,
            json={
                "contents":       [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0},  # deterministic output
            },
            timeout=15,
        )
        resp.raise_for_status()

        raw = (
            resp.json()
            .get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "[]")
            .strip()
        )
        # Strip markdown fences if Gemini wraps response in ```json ... ```
        raw      = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        projects = json.loads(raw)

        if isinstance(projects, list):
            print(f"   🤖 AI extracted {len(projects)} project(s) from email")
            return projects

    except Exception as e:
        print(f"   ⚠️ AI multi-project extraction failed: {e}")

    return []


# ══════════════════════════════════════════════════════════════════════════════
# SINGLE PROJECT DATA HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _find_field(text: str, *labels) -> str:
    """
    Extract a labelled field value from plain text (email body).
    Mirrors import_pdf.find_field() but limited to 200 chars for safety.
    Stops at the next "Label:" line to avoid capturing adjacent fields.
    """
    for label in labels:
        pattern = re.escape(label) + r"\s*[:\-]?\s*(.+)"
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            value = m.group(1).strip()
            value = re.split(r"\n[A-Z][a-zA-Z ]+:", value)[0].strip()
            return value[:200]
    return ""


def _parse_date(val: str):
    """
    Try parsing a date string in common formats.
    Returns datetime or None — same logic as import_pdf.parse_date().
    """
    if not val:
        return None
    for fmt in ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y"]:
        try:
            return datetime.strptime(val.strip(), fmt)
        except Exception:
            pass
    return None


def _build_gmail_query_for_project(project) -> str:
    """
    Build a Gmail search query for a project.
    project_id is searched everywhere; name-based terms are subject-only.
    Mirrors gmail_reader._build_gmail_query() for use in this module.
    """
    def _q(val):
        val = (val or "").strip()
        return f'"{val}"' if " " in val else val

    seen: set  = set()
    parts: list = []

    def _add_anywhere(val):
        v = (val or "").strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            parts.append(_q(v))

    def _add_subject(val):
        v = (val or "").strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            parts.append(f"subject:{_q(v)}")

    _add_anywhere(project.project_id)
    _add_subject(project.client_name)
    _add_subject(project.calendar_keyword)
    return " OR ".join(parts) if parts else project.project_id or ""


# ══════════════════════════════════════════════════════════════════════════════
# CONTACT UPSERT
# ══════════════════════════════════════════════════════════════════════════════

def _upsert_project_contacts(db, project, email_data: dict) -> list:
    """
    Extract and save team members from an email's From/To/CC fields.

    Role assignment:
      - Oneture sender (or Oneture CC/To person) → Project Manager
      - Non-Oneture, non-generic domain           → Client Contact
      - Everyone else                             → Team Member

    Existing members are updated (not duplicated).
    Returns list of newly added email addresses.
    """
    added    = []
    pm_name, pm_email = _identify_pm_from_email(email_data)

    # Save PM and update project.pm_email
    if pm_email:
        project.pm_email = pm_email
        existing_pm = db.query(Member).filter_by(project_id=project.id, email=pm_email).first()
        if existing_pm:
            existing_pm.name = pm_name or existing_pm.name
            existing_pm.role = "Project Manager"
        else:
            db.add(Member(
                project_id = project.id,
                name       = pm_name or pm_email.split("@")[0],
                email      = pm_email,
                role       = "Project Manager",
                source     = "email",
            ))

    seen = {pm_email}   # avoid adding PM again from CC/To

    for contact in email_data.get("cc_members", []) + email_data.get("to_members", []):
        email = contact["email"]
        if email in seen or not email:
            continue
        seen.add(email)

        # Determine role based on domain
        is_external = not any(email.endswith(d) for d in ONETURE_EMAIL_DOMAINS)
        domain      = email.split("@")[-1].lower() if "@" in email else ""
        is_generic  = domain in GENERIC_EMAIL_PROVIDERS
        role        = "Client Contact" if (is_external and not is_generic) else "Team Member"

        existing = db.query(Member).filter_by(project_id=project.id, email=email).first()
        if existing:
            # Never downgrade a PM role — only update if not already set
            if existing.role != "Project Manager":
                existing.role = existing.role or role
        else:
            db.add(Member(
                project_id = project.id,
                name       = contact["name"],
                email      = email,
                role       = role,
                source     = "email",
            ))
            added.append(email)

    return added


def _save_email_for_project(
    db, project, msg_id: str, email_data: dict, match_signal: str
) -> bool:
    """
    Classify and save one email to a project.

    Skips if already saved (checks both bare ID and project-prefixed ID).
    Returns True if saved, False if skipped (duplicate).
    """
    # Prefix with project ID so same Gmail message can belong to multiple projects
    storage_msg_id = f"{msg_id}:{project.project_id}"

    if db.query(Email).filter(
        Email.project_id == project.id,
        Email.gmail_msg_id.in_([msg_id, storage_msg_id])
    ).first():
        return False  # already saved

    subject  = email_data.get("subject", "") or "(no subject)"
    body     = email_data.get("body", "") or ""
    snippet  = email_data.get("snippet", "") or ""
    content  = body[:500] if body else snippet   # prefer body for classification

    category, risk = classify_email(subject, content)
    summary        = summarize_email(subject, content, category)

    db.add(Email(
        project_id      = project.id,
        gmail_msg_id    = storage_msg_id,
        gmail_thread_id = email_data.get("thread_id", ""),
        subject         = subject,
        sender          = email_data.get("sender_email", ""),
        received_at     = email_data.get("received_at") or datetime.utcnow(),
        category        = category,
        risk_signal     = risk,
        snippet         = snippet,
        summary         = summary,
        match_signal    = match_signal,
    ))
    return True


# ══════════════════════════════════════════════════════════════════════════════
# AUTO PROJECT CREATION
# ══════════════════════════════════════════════════════════════════════════════

def _auto_create_project_from_ai_data(ai_project: dict, email_data: dict, db) -> "Project":
    """
    Create a new project from AI-extracted data.
    Used when Gemini identified a new project discussion in an email.

    - System-generates the project ID (auto-increment)
    - Falls back to email-derived PM if AI didn't identify one
    - Sends PM confirmation email with magic link
    - Returns the created Project object (expunged from session)
    """
    project_id = _generate_project_id(db)

    # Prefer email-derived PM over AI guess — more reliable
    pm_name, pm_email = _identify_pm_from_email(email_data)
    if not ai_project.get("pm_email"):
        ai_project["pm_email"] = pm_email
        ai_project["pm_name"]  = pm_name

    client_name = (
        ai_project.get("client_name")
        or _identify_client_from_email(email_data)
        or "Unknown Client"
    )

    # Build Gmail query — use client name if known, else project ID alone
    keyword     = client_name if client_name != "Unknown Client" else project_id
    query_parts = list(dict.fromkeys(filter(None, [
        project_id,
        f'"{client_name}"' if " " in client_name else client_name
            if client_name != "Unknown Client" else None,
    ])))
    gmail_query = " OR ".join(query_parts)

    project = Project(
        project_id       = project_id,
        name             = ai_project.get("name") or f"Project {project_id}",
        client_name      = client_name,
        description      = ai_project.get("description") or email_data.get("subject", ""),
        delivery_lead    = ai_project.get("pm_name") or email_data.get("sender_name", ""),
        engagement       = ai_project.get("engagement") or "",
        start_date       = _parse_date(ai_project.get("start_date")),
        go_live_date     = _parse_date(ai_project.get("go_live_date")),
        gmail_query      = gmail_query,
        calendar_keyword = keyword,
        pm_email         = ai_project.get("pm_email") or "",
        delivery_pct     = 0,
        health           = "green",
        pm_confirmed     = False,
        pm_confirm_token = secrets.token_urlsafe(32),   # one-time magic link token
    )

    db.add(project)
    db.flush()   # get project.id before inserting related rows

    _upsert_project_contacts(db, project, email_data)

    # Always attach the two default governance rules
    for r in DEFAULT_RULES:
        db.add(GovernanceRule(
            project_id  = project.id,
            rule_type   = r["rule_type"],
            description = r["description"],
            sla_hours   = r["sla_hours"],
            frequency   = r["frequency"],
        ))

    db.commit()
    db.refresh(project)

    print(f"✅ Auto-created project {project_id}: \"{project.name}\" (Client: {client_name})")

    # Send PM confirmation email — PM clicks the link to verify project details
    member_emails = [
        m.email for m in db.query(Member).filter_by(project_id=project.id).all()
        if m.email and m.email != project.pm_email
    ]
    try:
        send_pm_confirmation_request(project, project.pm_email, member_emails)
        print(f"📨 PM confirmation sent to {project.pm_email}")
    except Exception as e:
        print(f"⚠️ PM confirmation email failed: {e}")

    # Expunge so caller can use the object after this session closes
    db.expunge(project)
    return project


def _auto_create_project_legacy(project_id: str, email_data: dict) -> "Project":
    """
    Legacy creation path: email contained an explicit PRJ-XXXX ID.
    Creates the project using that exact ID (no auto-increment).

    Falls back to regex field parsing from email body for metadata.
    Returns existing project if it already exists (idempotent).
    """
    db       = Session()
    existing = db.query(Project).filter_by(project_id=project_id.upper()).first()
    if existing:
        db.close()
        return existing   # already exists — nothing to create

    subject   = email_data.get("subject", "")
    body      = email_data.get("body", "")
    snippet   = email_data.get("snippet", "")
    text      = f"{subject}\n{body}\n{snippet}"
    pid_upper = project_id.upper()

    pm_name, pm_email = _identify_pm_from_email(email_data)
    client_name = (
        _find_field(text, "Client Name", "Client:")
        or _identify_client_from_email(email_data)
        or "Unknown Client"
    )

    # Derive project name from subject by stripping the PRJ-XXXX prefix/suffix
    cleaned = re.sub(
        r"[\[\(]?" + re.escape(pid_upper) + r"[\]\)]?", "", subject, flags=re.IGNORECASE
    ).strip()
    cleaned = re.sub(r"^[\s|\-:\u2013\u2014]+", "", cleaned).strip()
    cleaned = re.sub(r"[\s|\-:\u2013\u2014]+$", "", cleaned).strip()
    noise   = {"update", "re", "fwd", "fw", "follow up", "follow-up", "hi", "hello"}
    # Use cleaned subject as name only if meaningful; otherwise fall back to "Project PRJ-XXXX"
    name    = (
        cleaned if cleaned and len(cleaned) >= 4 and cleaned.lower() not in noise
        else f"Project {pid_upper}"
    )

    keyword     = client_name if client_name != "Unknown Client" else pid_upper
    query_parts = list(dict.fromkeys(filter(None, [
        f"subject:{pid_upper}", pid_upper,
        f'"{client_name}"' if client_name != "Unknown Client" else None,
    ])))

    project = Project(
        project_id       = pid_upper,
        name             = name,
        client_name      = client_name,
        description      = _find_field(text, "Description") or subject,
        delivery_lead    = _find_field(text, "Delivery Lead", "Lead") or pm_name,
        engagement       = _find_field(text, "Engagement", "Contract") or "",
        start_date       = _parse_date(_find_field(text, "Start Date", "Start")),
        go_live_date     = _parse_date(_find_field(text, "Go-Live Date", "GoLive", "Deadline")),
        gmail_query      = " OR ".join(query_parts),
        calendar_keyword = keyword,
        pm_email         = pm_email,
        delivery_pct     = 0,
        health           = "green",
        pm_confirmed     = False,
        pm_confirm_token = secrets.token_urlsafe(32),
    )

    db.add(project)
    db.commit()
    db.refresh(project)

    _upsert_project_contacts(db, project, email_data)

    for r in DEFAULT_RULES:
        db.add(GovernanceRule(
            project_id  = project.id,
            rule_type   = r["rule_type"],
            description = r["description"],
            sla_hours   = r["sla_hours"],
            frequency   = r["frequency"],
        ))
    db.commit()

    # Send PM confirmation
    member_emails = [
        m.email for m in db.query(Member).filter_by(project_id=project.id).all()
        if m.email and m.email != project.pm_email
    ]
    try:
        send_pm_confirmation_request(project, project.pm_email, member_emails)
        print(f"📨 PM confirmation sent to {project.pm_email}")
    except Exception as e:
        print(f"⚠️ PM confirmation email failed: {e}")

    print(f"✅ Project auto-created (legacy): {project.project_id}")
    db.refresh(project)
    db.expunge(project)   # detach so object survives session close
    db.close()
    return project


# ══════════════════════════════════════════════════════════════════════════════
# PROJECT MATCHING HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _normalize_match_text(value: str) -> str:
    """
    Normalize text for fuzzy matching:
    lowercase → strip non-alphanumeric → collapse whitespace.
    e.g. "Flipkart Ads!" → "flipkart ads"
    """
    value = (value or "").lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _extract_project_label(text: str) -> str:
    """
    Look for an explicit project label in the email text such as:
      "Project Name: Flipkart Ads"
      "Project: Retail ERP"
      "Update for Oneture CRM:"

    Returns the extracted label string, or "" if none found.
    A found label becomes a hard routing constraint — it overrides weaker signals.
    """
    patterns = [
        r"^\s*Project\s+Name\s*[:\-]\s*(.+)$",
        r"^\s*Project\s*[:\-]\s*(.+)$",
        r"^\s*For\s+Project\s*[:\-]\s*(.+)$",
        r"^\s*Update\s+for\s+(.+?)\s*[:\-]?\s*$",
    ]
    for line in (text or "").splitlines():
        clean = line.strip()
        if not clean:
            continue
        for pattern in patterns:
            m = re.match(pattern, clean, re.IGNORECASE)
            if m:
                value = re.split(r"\s{2,}|[|]", m.group(1).strip())[0].strip()
                value = re.sub(r"[\.;,]+$", "", value).strip()
                if len(value) >= 4:
                    return value[:160]
    return ""


def _match_project_by_explicit_label(text: str) -> tuple:
    """
    Try to match email to a project using an explicit "Project Name:" label.

    Returns (project_id, label_was_seen):
      - (project_id, True)  → unambiguous match found
      - (None, True)        → label found but ambiguous/unmatched → stop routing
      - (None, False)       → no label found → continue to next matcher
    """
    label = _extract_project_label(text)
    if not label:
        return None, False   # no label present

    label_norm = _normalize_match_text(label)
    db = Session()
    try:
        matches = []
        for project in db.query(Project).all():
            project_name     = _normalize_match_text(project.name)
            calendar_keyword = _normalize_match_text(project.calendar_keyword)

            if label_norm and label_norm == project_name:
                matches.append((project, "project_name_exact"))
            elif label_norm and project_name and (
                label_norm in project_name or project_name in label_norm
            ):
                matches.append((project, "project_name_phrase"))
            elif label_norm and calendar_keyword and label_norm == calendar_keyword:
                matches.append((project, "calendar_keyword_exact"))

        project_ids = {project.id for project, _ in matches}

        if len(project_ids) == 1:
            project, reason = matches[0]
            print(f"🎯 Explicit label matched {project.project_id} ({reason}: {label})")
            return project.project_id, True

        if len(project_ids) > 1:
            print(f"🎯 Explicit label ambiguous ({label}); leaving email unmapped")
        else:
            print(f"🎯 Explicit label not found ({label}); leaving email unmapped")

        # Label was present but couldn't be resolved — stop routing to avoid false matches
        return None, True
    finally:
        db.close()


def _match_project_by_name(text: str):
    """
    Score all projects against the email text using name/keyword/client signals.

    Scoring weights:
      project_name     : 5 points (strongest — specific to one project)
      calendar_keyword : 1 point  (weaker — shared keywords possible)
      client_name      : 0 points if shared across multiple projects (ambiguous)

    Only accepts a match if:
      - The best project has a project_name hit (weight > 0)
      - Score is at least 5
      - No other project ties for the top score

    Returns project_id string or None.
    """
    db = Session()
    try:
        projects     = db.query(Project).all()
        haystack     = text.lower()

        # Count how many projects share each client name — shared names score 0
        client_counts = {}
        for p in projects:
            client = (p.client_name or "").strip().lower()
            if client:
                client_counts[client] = client_counts.get(client, 0) + 1

        scored = []
        for p in projects:
            matches = []
            for label, sig, weight in [
                ("project_name",     p.name,             5),
                ("calendar_keyword", p.calendar_keyword, 1),
                ("client_name",      p.client_name,      0),
            ]:
                val = (sig or "").strip()
                if len(val) < 4 or val.lower() not in haystack:
                    continue
                # Client name shared across projects → worthless as signal
                if label == "client_name" and client_counts.get(val.lower(), 0) > 1:
                    matches.append((label, val, 0))
                else:
                    matches.append((label, val, weight))

            score = sum(m[2] for m in matches)
            if score > 0:
                scored.append((score, p, matches))

        if not scored:
            return None

        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best_project, best_matches = scored[0]
        tied = [item for item in scored if item[0] == best_score]

        # Reject if: no project_name hit, low score, or multiple projects tied
        has_project_name = any(
            label == "project_name" and weight > 0
            for label, _, weight in best_matches
        )
        if not has_project_name or best_score < 5 or len(tied) > 1:
            print("🔍 Name/client match ambiguous; leaving email unmapped")
            return None

        signal = ", ".join(
            f"{label}:{val}" for label, val, _ in best_matches if _ > 0
        )
        print(f"🔍 Matched project {best_project.project_id} via {signal}")
        return best_project.project_id
    finally:
        db.close()


def _match_project_by_saved_thread(thread_id: str):
    """
    Match an email to a project by its Gmail thread ID.

    If GovTrack has already saved any email from this thread, later replies
    in the same thread are automatically routed to the same project.
    This handles reply chains where the PRJ-ID appears only in the first message.

    Returns project_id string or None.
    Rejects if the thread maps to multiple projects (ambiguous).
    """
    if not thread_id:
        return None

    db = Session()
    try:
        saved_rows = (
            db.query(Email)
            .filter(Email.gmail_thread_id == thread_id)
            .order_by(Email.received_at.desc())
            .all()
        )
        project_ids = {row.project_id for row in saved_rows}

        if not project_ids:
            return None

        if len(project_ids) > 1:
            # Thread spans multiple projects — too risky to auto-assign
            projects = db.query(Project).filter(Project.id.in_(project_ids)).all()
            labels   = ", ".join(p.project_id for p in projects)
            print(f"🧵 Thread maps to multiple projects ({labels}); not auto-assigning")
            return None

        project = db.query(Project).filter_by(id=next(iter(project_ids))).first()
        if project:
            print(f"🧵 Matched project {project.project_id} via saved Gmail thread")
            return project.project_id
    finally:
        db.close()
    return None


def _get_thread_text(service, thread_id: str, current_msg_id: str = "") -> str:
    """
    Fetch the last 6 messages of a Gmail thread and return them as combined text.
    Used to find PRJ-IDs or project signals in earlier messages of a reply chain.

    Labels each message as CURRENT (the triggering email) or THREAD (earlier reply).
    Returns "" if thread fetch fails.
    """
    if not thread_id:
        return ""
    try:
        thread = service.users().threads().get(
            userId="me", id=thread_id, format="full"
        ).execute()
    except Exception as e:
        print(f"   ⚠️ Could not read Gmail thread: {e}")
        return ""

    chunks = []
    # Only last 6 messages to stay within Gemini context limits
    for msg in thread.get("messages", [])[-6:]:
        payload = msg.get("payload", {})
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
        subject = headers.get("Subject", "")
        snippet = msg.get("snippet", "")
        body    = ""
        try:
            body = _extract_body_from_payload(payload)
        except Exception:
            pass
        prefix = "CURRENT" if msg.get("id") == current_msg_id else "THREAD"
        chunks.append(
            f"{prefix} Subject: {subject}\nSnippet: {snippet}\nBody: {body[:700]}"
        )
    return "\n\n".join(chunks)


def _project_ai_payload(project) -> dict:
    """
    Build a compact project summary dict for the AI matching prompt.
    Only includes fields useful for semantic matching — keeps prompt size small.
    """
    return {
        "project_id":       project.project_id,
        "name":             project.name or "",
        "client_name":      project.client_name or "",
        "description":      (project.description or "")[:300],
        "calendar_keyword": project.calendar_keyword or "",
        "gmail_query":      project.gmail_query or "",
        "pm_email":         project.pm_email or "",
    }


def _ai_match_project(email_data: dict, thread_text: str = ""):
    """
    Ask Gemini to match an email to an existing project.

    Uses the 40 most recently created projects as candidates.
    Requires GEMINI_API_KEY — returns None if not set.

    Safety check: even if AI returns a project_id with high confidence,
    the match is rejected if neither the project name nor calendar_keyword
    appears literally in the email text. This prevents hallucinated matches.

    Returns project_id string or None.
    """
    if not GEMINI_API_KEY:
        return None

    db = Session()
    try:
        # Limit to 40 most recent projects to keep prompt size manageable
        projects       = db.query(Project).order_by(Project.created_at.desc()).limit(40).all()
        project_payload = [_project_ai_payload(p) for p in projects]
        valid_ids       = {p["project_id"].upper() for p in project_payload}
    finally:
        db.close()

    if not project_payload:
        return None

    email_payload = {
        "subject":        email_data.get("subject", ""),
        "sender_name":    email_data.get("sender_name", ""),
        "sender_email":   email_data.get("sender_email", ""),
        "cc_members":     email_data.get("cc_members", []),
        "snippet":        email_data.get("snippet", "")[:700],
        "body":           email_data.get("body", "")[:1800],
        "thread_context": thread_text[:3500],
    }

    prompt = (
        "You are GovTrack's project email router. Match the email to exactly one "
        "existing project only when the meaning, client, people, thread history, "
        "delivery topic, or project context clearly belongs to that project. "
        "If the email is unrelated, promotional, personal, or uncertain, use NONE.\n\n"
        "Return only strict JSON with keys: project_id, confidence, reason.\n"
        "confidence must be 0-100. project_id must be one of the provided IDs or NONE.\n\n"
        f"Projects:\n{json.dumps(project_payload, ensure_ascii=False)}\n\n"
        f"Email:\n{json.dumps(email_payload, ensure_ascii=False)}"
    )

    try:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
        )
        resp = requests.post(
            url,
            json={
                "contents":         [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature":    0,                    # deterministic output
                    "responseMimeType": "application/json", # force JSON response
                },
            },
            timeout=12,
        )
        resp.raise_for_status()

        raw = (
            resp.json().get("candidates", [{}])[0]
            .get("content", {}).get("parts", [{}])[0]
            .get("text", "{}").strip()
        )
        data       = json.loads(raw)
        project_id = str(data.get("project_id", "NONE")).upper().strip()
        confidence = int(float(data.get("confidence", 0)))
        reason     = str(data.get("reason", ""))[:140]

        if project_id in valid_ids and confidence >= AI_MATCH_CONFIDENCE_THRESHOLD:
            chosen = next(
                (p for p in project_payload if p["project_id"].upper() == project_id), None
            )
            # Build normalized evidence string from all email text
            evidence = _normalize_match_text(
                " ".join([
                    email_data.get("subject", ""),
                    email_data.get("snippet", ""),
                    email_data.get("body", ""),
                    thread_text or "",
                ])
            )
            # Verify AI choice has literal evidence in the email
            # (guards against hallucinated matches based on general context alone)
            strong_terms = [
                _normalize_match_text(chosen.get("name", "")) if chosen else "",
                _normalize_match_text(chosen.get("calendar_keyword", "")) if chosen else "",
            ]
            has_literal_evidence = any(
                term and len(term) >= 4 and term in evidence
                for term in strong_terms
            )
            if not has_literal_evidence:
                print(
                    f"🤖 AI chose {project_id} but no literal evidence found; leaving unmapped"
                )
                return None

            print(f"🤖 AI matched project {project_id} ({confidence}%): {reason}")
            return project_id

        print(f"🤖 AI did not assign project ({project_id}, {confidence}%): {reason}")

    except Exception as e:
        print(f"   ⚠️ AI project match failed: {e}")

    return None


# ══════════════════════════════════════════════════════════════════════════════
# ONETURE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _is_oneture_email(email: str) -> bool:
    """Return True if the email address belongs to an Oneture domain."""
    return any((email or "").strip().lower().endswith(d) for d in ONETURE_EMAIL_DOMAINS)


def _sender_domain(email: str) -> str:
    match = re.search(r"@([\w.\-]+)", email or "")
    return match.group(1).lower() if match else ""


def _is_noise_email(email_data: dict) -> bool:
    subject = (email_data.get("subject") or "").lower()
    sender_domain = _sender_domain(email_data.get("sender_email", ""))
    if sender_domain in NOISE_SENDER_DOMAINS:
        return True
    return any(re.search(pattern, subject) for pattern in NOISE_SUBJECT_PATTERNS)


def _sender_is_oneture(email_data: dict) -> bool:
    """Return True if the email was sent FROM an Oneture address."""
    return _is_oneture_email(email_data.get("sender_email", ""))


def _find_oneture_alert_recipient(email_data: dict) -> str:
    sender_email = (email_data.get("sender_email") or "").strip().lower()
    if _is_oneture_email(sender_email):
        return sender_email
    for member in email_data.get("to_members", []) + email_data.get("cc_members", []):
        email = (member.get("email") or "").strip().lower()
        if _is_oneture_email(email):
            return email
    return ""


def _involves_oneture(email_data: dict) -> bool:
    """
    Return True if any address in From/To/CC is an Oneture address.
    Used as the primary gate — emails with no Oneture involvement are skipped.
    """
    all_addresses = (
        [email_data.get("sender_email", "")]
        + [m["email"] for m in email_data.get("to_members", [])]
        + [m["email"] for m in email_data.get("cc_members", [])]
    )
    return any(_is_oneture_email(addr) for addr in all_addresses)


# ══════════════════════════════════════════════════════════════════════════════
# UNMAPPED EMAIL HANDLER
# ══════════════════════════════════════════════════════════════════════════════

def _handle_unmapped_email(service, msg_id: str, email_data: dict):
    """
    Handle a work-related email that matched no existing project.

    Steps:
      1. Skip if sender is not @oneture.com (only Oneture staff trigger alerts)
      2. Save to unmapped_emails table (or find existing record)
      3. Send an alert email to the Oneture sender so they can assign it

    Skips silently if the alert was already sent (idempotent).
    """
    oneture_recipient = _find_oneture_alert_recipient(email_data)
    if not oneture_recipient:
        print(f"   ↳ Skipped unmapped alert (no @oneture.com recipient): "
              f"{email_data.get('subject','')[:60]}")
        return

    db = Session()
    try:
        existing = db.query(UnmappedEmail).filter_by(gmail_msg_id=msg_id).first()
        subject  = email_data.get("subject", "")
        sender   = email_data.get("sender_email", "")
        snippet  = email_data.get("snippet", "")
        body     = email_data.get("body", "")

        if existing:
            if existing.alert_sent:
                return   # alert already fired — nothing to do
            record = existing
        else:
            # First time seeing this email — save it
            record = UnmappedEmail(
                gmail_msg_id    = msg_id,
                gmail_thread_id = email_data.get("thread_id", ""),
                subject         = subject,
                sender          = sender,
                snippet         = snippet,
                body_preview    = body[:500],   # first 500 chars for manager preview
                received_at     = datetime.utcnow(),
                alert_sent      = False,
            )
            db.add(record)
            db.commit()
            db.refresh(record)
            print(f"📥 Unmapped email saved: \"{subject[:60]}\" from {sender}")

        # Send alert to the Oneture sender so they can assign it from the dashboard
        try:
            from govtrack.services.Notifier import send_unassigned_email_alert
            send_unassigned_email_alert(record, oneture_recipient)
            record.alert_sent = True
            db.commit()
            print(f"📨 Unassigned email alert sent to {oneture_recipient}")
        except Exception as e:
            print(f"⚠️ Unassigned alert email failed: {e}")

    except Exception as e:
        print(f"❌ _handle_unmapped_email error: {e}")
        db.rollback()
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════════════════════
# PROJECT RESOLVER (6-LAYER ENGINE)
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_project_for_email(
    service, msg_id: str, email_data: dict, text: str
) -> tuple:
    """
    Resolve which project an email belongs to using a 6-layer matching engine.
    Layers are ordered from most definitive to least — first match wins.

    Layer 1 — Explicit PRJ-XXXX in current email text
    Layer 2 — Explicit "Project Name:" label in email body
    Layer 3 — PRJ-XXXX found in earlier messages of the same Gmail thread
    Layer 4 — Gmail thread ID already saved in the DB (reply chain routing)
    Layer 5 — Project name / client / keyword literal match
    Layer 6 — Gemini semantic classification (only if Oneture involved or work signal)

    Returns (project_id: str | None, match_source: str | None)
    """

    # ── Layer 1: Explicit project ID in current email ─────────────────────────
    match = PROJECT_ID_PATTERN.search(text)
    if match:
        return match.group(0).upper(), "current_id"

    # ── Layer 2: Explicit project label e.g. "Project Name: Flipkart Ads" ─────
    explicit_project_id, explicit_label_seen = _match_project_by_explicit_label(text)
    if explicit_project_id:
        return explicit_project_id, "explicit_project_label"
    if explicit_label_seen:
        # Label was present but unresolvable — stop here to avoid wrong match
        return None, None

    # ── Layer 3: PRJ-XXXX in earlier thread messages ──────────────────────────
    thread_text  = _get_thread_text(service, email_data.get("thread_id", ""), msg_id)
    thread_match = PROJECT_ID_PATTERN.search(thread_text)
    if thread_match:
        pid = thread_match.group(0).upper()
        print(f"🧵 Thread matched project {pid}")
        return pid, "thread_id"

    # ── Layer 4: Thread already saved in DB ───────────────────────────────────
    project_id = _match_project_by_saved_thread(email_data.get("thread_id", ""))
    if project_id:
        return project_id, "saved_thread"

    # ── Layer 5: Literal name / keyword / client match ────────────────────────
    project_id = _match_project_by_name(text)
    if project_id:
        return project_id, "name_signal"

    # ── Layer 6: AI semantic classification ───────────────────────────────────
    # Only attempted if there's Oneture involvement OR explicit work signals
    # to avoid burning API calls on clearly irrelevant emails
    lowered         = f"{text}\n{thread_text}".lower()
    has_work_signal = any(k in lowered for k in PROJECT_SIGNAL_KEYWORDS)
    if _involves_oneture(email_data) or has_work_signal:
        project_id = _ai_match_project(email_data, thread_text)
        if project_id:
            return project_id, "ai_semantic"

    return None, None


# ══════════════════════════════════════════════════════════════════════════════
# WATCHER LOOP
# ══════════════════════════════════════════════════════════════════════════════

def _watch_loop():
    """
    Main polling loop. Runs in a background daemon thread.

    Each cycle:
      1. Fetch up to 50 unprocessed inbox emails
      2. Skip emails with no Oneture address
      3. Try to match each email to an existing project (6-layer engine)
      4. If matched → save email + sync emails/meetings for that project
      5. If unmatched → check for work signals → run AI extraction
      6. If AI finds projects → auto-create them
      7. If AI finds nothing → save as unmapped + alert manager
      8. Mark every processed message with the GovTrack Gmail label
      9. Sleep POLL_INTERVAL_SECONDS before next cycle
    """
    print("👀 GovTrack email watcher started (v2.0 — multi-project extraction)...")

    service = gmail_service()

    try:
        label_id = _get_or_create_label(service)
    except Exception as e:
        print(f"❌ Gmail watcher could not start: {e}")
        print("   Delete token.json and re-run the app to re-authenticate.")
        return

    while not _stop_event.is_set():
        try:
            print("📨 Checking inbox...")

            result = service.users().messages().list(
                userId="me", q=WATCHER_QUERY, maxResults=50,
            ).execute()

            for msg in result.get("messages", []):
                msg_id = msg["id"]
                try:
                    email_data = _get_full_email(service, msg_id)

                    if _is_noise_email(email_data):
                        print(f"⏭️ Ignored noise email: "
                              f"{email_data.get('subject','')[:60]}")
                        _mark_seen_by_govtrack(service, msg_id, label_id)
                        continue

                    # Gate: skip entirely if no Oneture address involved
                    if not _involves_oneture(email_data):
                        print(f"⏭️ Ignored non-Oneture email: "
                              f"{email_data.get('subject','')[:60]}")
                        _mark_seen_by_govtrack(service, msg_id, label_id)
                        continue

                    subject = email_data.get("subject", "") or ""
                    body    = email_data.get("body", "") or ""
                    snippet = email_data.get("snippet", "") or ""
                    text    = f"{subject}\n{body}\n{snippet}"
                    lowered = text.lower()

                    # ── Step 1: Match to an existing project ──────────────────
                    project_id, match_source = _resolve_project_for_email(
                        service, msg_id, email_data, text,
                    )

                    if project_id:
                        print(f"📌 Found project ID: {project_id} via {match_source}")
                        db = Session()
                        existing = db.query(Project).filter_by(project_id=project_id).first()
                        db.close()

                        if existing:
                            # Project exists → save email + sync all emails/meetings
                            print(f"✅ Existing project found: {project_id}")
                            db = Session()
                            live = db.query(Project).filter_by(project_id=project_id).first()
                            if live:
                                added = _upsert_project_contacts(db, live, email_data)
                                saved = _save_email_for_project(db, live, msg_id, email_data, match_source)
                                db.commit()
                                if saved:
                                    print("📥 Saved current email to matched project")
                                if added:
                                    print(f"👥 Added {len(added)} new contact(s)")
                            db.close()
                            # Full sync to pick up any other matching emails/meetings
                            fetch_emails(existing)
                            fetch_meetings(existing)

                        else:
                            # PRJ-ID found but project doesn't exist yet → create it
                            print(f"🆕 New project (explicit ID): {project_id}")
                            project = _auto_create_project_legacy(project_id, email_data)
                            if project:
                                db = Session()
                                live = db.query(Project).filter_by(
                                    project_id=project.project_id
                                ).first()
                                if live:
                                    _save_email_for_project(db, live, msg_id, email_data, match_source)
                                    db.commit()
                                db.close()
                                fetch_emails(project)
                                fetch_meetings(project)

                        _mark_processed(service, msg_id, label_id)
                        continue

                    # ── Step 2: No project matched — check if worth processing ─
                    has_work       = any(k in lowered for k in PROJECT_SIGNAL_KEYWORDS)
                    is_oneture_sender = _sender_is_oneture(email_data)

                    if not (has_work or is_oneture_sender):
                        # No signals and not from Oneture — silently skip
                        print(f"⏭️ No project signals, skipping: {subject[:60]}")
                        _mark_seen_by_govtrack(service, msg_id, label_id)
                        continue

                    # ── Step 3: AI multi-project extraction ───────────────────
                    print(f"🔍 Running AI multi-project extraction: \"{subject[:60]}\"")
                    ai_projects = _ai_extract_multiple_projects(email_data)

                    if not ai_projects:
                        # AI found nothing — save as unmapped and alert manager
                        _handle_unmapped_email(service, msg_id, email_data)
                        _mark_processed(service, msg_id, label_id)
                        continue

                    # ── Step 4: Create each AI-extracted project ──────────────
                    print(f"🆕 Creating {len(ai_projects)} new project(s) from email")
                    db = Session()
                    created_projects = []
                    try:
                        for ai_proj in ai_projects:
                            proj = _auto_create_project_from_ai_data(ai_proj, email_data, db)
                            created_projects.append(proj)
                            live = db.query(Project).filter_by(
                                project_id=proj.project_id
                            ).first()
                            if live:
                                _save_email_for_project(db, live, msg_id, email_data, "ai_extracted")
                                db.commit()
                    except Exception as e:
                        print(f"❌ Project creation error: {e}")
                        db.rollback()
                    finally:
                        db.close()

                    # Sync emails/meetings for all newly created projects
                    for proj in created_projects:
                        try:
                            fetch_emails(proj)
                            fetch_meetings(proj)
                        except Exception as e:
                            print(f"⚠️ fetch error for {proj.project_id}: {e}")

                    _mark_processed(service, msg_id, label_id)

                except Exception as e:
                    print(f"❌ Message processing failed: {e}")

        except Exception as e:
            print(f"❌ WATCHER ERROR: {e}")

        time.sleep(POLL_INTERVAL_SECONDS)


# ══════════════════════════════════════════════════════════════════════════════
# THREAD CONTROL
# ══════════════════════════════════════════════════════════════════════════════

def start_watcher():
    """
    Start the email watcher in a background daemon thread.
    Does nothing if the thread is already running.
    Daemon=True ensures the thread dies automatically when the main app exits.
    """
    global _watcher_thread
    if _watcher_thread and _watcher_thread.is_alive():
        return   # already running
    _stop_event.clear()
    _watcher_thread = threading.Thread(target=_watch_loop, daemon=True)
    _watcher_thread.start()


def stop_watcher():
    """Signal the watcher thread to stop after its current poll cycle."""
    _stop_event.set()


def watcher_running() -> bool:
    """Return True if the watcher thread is currently running."""
    return _watcher_thread is not None and _watcher_thread.is_alive()


# ══════════════════════════════════════════════════════════════════════════════
# STANDALONE RUN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Run the watcher directly from terminal: python Email_watcher.py
    # Ctrl+C to stop cleanly.
    start_watcher()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_watcher()
        print("🛑 Watcher stopped.")
