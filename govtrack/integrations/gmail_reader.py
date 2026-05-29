"""
gmail_reader.py — Gmail email fetcher for GovTrack.

Fetches emails per project using a smart Gmail query, then runs each
email through a 3-layer relevance filter before classifying and saving.

Layer 0 — Noise block  : instantly rejects known junk senders/subjects
Layer 1 — Keyword gate : accepts emails with project ID or work keywords
Layer 2 — AI gate      : configured LLM decides genuinely ambiguous cases
"""

import re
import base64
from datetime import datetime
from govtrack.core.models import Session, Email
from govtrack.core.google_auth import gmail_service
from govtrack.ai.email_rules import classify_email, summarize_email
from govtrack.ai.llm_provider import llm_json
from dotenv import load_dotenv
from govtrack.core.paths import ENV_PATH

load_dotenv(ENV_PATH)

# Only emails that involve at least one Oneture address are processed.
# Typo variant "onerute.com" is intentionally included for resilience.
ONETURE_EMAIL_DOMAINS = ("@oneture.com", "@onerute.com")


# ══════════════════════════════════════════════════════════════════════════════
# KEYWORD & NOISE LISTS
# ══════════════════════════════════════════════════════════════════════════════

# Layer 1 gate: email must contain at least one of these to be accepted
# when matched by name (not by explicit project ID).
WORK_KEYWORDS = [
    # Governance
    "mom", "minutes", "action items", "meeting notes", "wbr",
    "weekly review", "status report",
    # Delivery
    "delivery", "milestone", "go-live", "golive", "deployment", "release",
    "delay", "blocker", "at risk", "timeline", "escalation", "sla",
    # Project management
    "project", "kickoff", "kick-off", "engagement", "scope", "requirement",
    "migration", "implementation", "integration", "uat", "testing",
    "sign-off", "signoff", "approval", "invoice", "contract",
]

# Layer 0: subject patterns that are always noise — rejected before any
# keyword or AI check. Uses regex so partial matches work (e.g. "coupon|promo").
NOISE_SUBJECT_PATTERNS = [
    r"^re:\s*$",                    # blank reply subject
    r"linkedin",
    r"unsubscribe",
    r"newsletter",
    r"invitation.*meetup",          # social meetups, not project meetings
    r"weekly meetup",
    r"nyka parchi",                 # known false positive
    r"youtube recommendation",
    r"flipkart ads",                # ad platform emails, not project comms
    r"cancelled event",
    r"canceled event",
    # Promotional / marketing patterns
    r"scholarship",
    r"cheat sheet",
    r"crack.*interview",
    r"skipping your profile",
    r"digest",                      # news digests e.g. Groww Digest
    r"applications closing",
    r"deadline extended",
    r"job alert",
    r"hiring.*now",
    r"you.*shortlisted",
    r"congratulations.*selected",
    r"coupon|promo code|discount|offer|deal",
    r"don't miss|limited time|last chance",
    r"open this email",
    r"unsubscribe|opt.?out",
]

# Layer 0: sender domains that always produce noise — all their emails are
# skipped immediately without subject or keyword checks.
NOISE_SENDER_DOMAINS = {
    "buddy4study.com", "unstop.com", "dare2compete.com",
    "groww.in", "reddit.com", "naukri.com", "foundit.in",
    "shine.com", "internshala.com", "youtube.com",
    "linkedin.com", "newsletters.linkedin.com", "mail.linkedin.com",
}


# ══════════════════════════════════════════════════════════════════════════════
# BODY EXTRACTOR
# ══════════════════════════════════════════════════════════════════════════════

def _get_body(payload: dict) -> str:
    """
    Recursively extract plain-text body from a Gmail message payload.
    Gmail nests the body inside parts for multipart messages — this walks
    the tree until it finds a text/plain part with base64 data.
    Returns empty string if no plain-text body is found.
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

    return ""


# ══════════════════════════════════════════════════════════════════════════════
# GMAIL QUERY BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _build_gmail_query(project) -> str:
    """
    Build a Gmail search query for a project.

    Strategy:
      - project_id        → search ANYWHERE (body + subject): very specific,
                            low false-positive risk e.g. "PRJ-1001"
      - client_name       → SUBJECT ONLY via "subject:" prefix: prevents Gmail
      - calendar_keyword    returning promo emails that merely mention the
      - project_name        client name somewhere deep in the body.

    Multi-word values are quoted so Gmail treats them as a phrase.
    Duplicate terms are deduplicated via the `seen` set.
    """
    def _q(val: str) -> str:
        """Wrap multi-word values in quotes for Gmail phrase search."""
        val = (val or "").strip()
        return f'"{val}"' if " " in val else val

    seen: set  = set()
    parts: list = []

    def _add_anywhere(val: str):
        """Add a term that Gmail will search in all fields."""
        v = (val or "").strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            parts.append(_q(v))

    def _add_subject(val: str):
        """Add a term restricted to the subject line only."""
        v = (val or "").strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            parts.append(f"subject:{_q(v)}")

    _add_anywhere(project.project_id)    # unique ID — safe to search everywhere
    _add_subject(project.client_name)    # name-based — subject only to cut noise
    _add_subject(project.calendar_keyword)

    return " OR ".join(parts) if parts else project.project_id or ""


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 0 — NOISE BLOCK
# ══════════════════════════════════════════════════════════════════════════════

def _sender_domain(sender: str) -> str:
    """Extract bare domain from a From header e.g. 'Name <addr@domain.com>' → 'domain.com'."""
    match = re.search(r"@([\w.\-]+)", sender or "")
    return match.group(1).lower() if match else ""


def _has_oneture_address(*header_values: str) -> bool:
    """Return True if any of the given header strings contain an Oneture email address."""
    text = " ".join(v or "" for v in header_values).lower()
    return any(domain in text for domain in ONETURE_EMAIL_DOMAINS)


def _is_noise(subject: str, sender: str = "") -> bool:
    """
    Return True if this email is known noise and should be rejected immediately.
    Checks sender domain first (cheapest), then subject patterns.
    Called before any keyword or AI check to avoid wasted processing.
    """
    if _sender_domain(sender) in NOISE_SENDER_DOMAINS:
        return True
    s = subject.lower()
    return any(re.search(p, s) for p in NOISE_SUBJECT_PATTERNS)


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 1 — KEYWORD GATE
# ══════════════════════════════════════════════════════════════════════════════

def _has_work_signal(text: str) -> bool:
    """Return True if the combined subject+snippet+body contains a work keyword."""
    return any(kw in text.lower() for kw in WORK_KEYWORDS)


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 2 — AI RELEVANCE GATE
# ══════════════════════════════════════════════════════════════════════════════

def _ai_is_project_email(
    subject: str, snippet: str,
    project_name: str, client_name: str
) -> bool:
    """
    Ask the configured LLM whether this email is genuinely project-related.

    Only called when:
      - The email matched by name (not by explicit project ID), AND
      - Layer 1 keyword gate did NOT fire (genuinely ambiguous content)

    Returns True (keep) or False (discard).
    Falls back to False on any API error — conservative approach
    ensures we never add noise to the project feed.
    """
    prompt = (
        f"You are a delivery governance assistant.\n"
        f"Project: \"{project_name}\" | Client: \"{client_name}\"\n\n"
        f"Email subject: {subject}\n"
        f"Email snippet: {snippet[:300]}\n\n"
        f"Is this email genuinely related to this project's delivery, "
        f"governance, meetings, risks, or client communication?\n"
        f"Return only JSON like: {{\"is_project_email\": true}}."
    )

    try:
        answer = llm_json(prompt, fallback={"is_project_email": False})
        return bool(answer.get("is_project_email", False))

    except Exception as e:
        print(f"     ⚠️  LLM relevance check failed: {e} — discarding email")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# COMBINED RELEVANCE FILTER
# ══════════════════════════════════════════════════════════════════════════════

def _check_relevance(
    subject: str, snippet: str, body: str,
    project, sender: str = ""
) -> tuple[bool, str, str]:
    """
    Run an email through the 3-layer relevance filter.

    Returns: (relevant: bool, match_signal: str | None, reason: str)
      - relevant      : whether to save this email to the project
      - match_signal  : what field matched (project_id / client_name / etc.)
      - reason        : human-readable decision reason for terminal logging

    Decision order (first match wins):
      1. Project ID found anywhere          → accept immediately
      2. Known noise subject/sender         → reject immediately
      3. Name found in subject line         → accept (strong signal)
      4. Sender domain matches client       → accept (strong signal)
      5. Name found + work keyword in body  → accept (Layer 1)
      6. Name found but no work keyword     → ask LLM (Layer 2)
      7. No name signal at all              → reject
    """
    full_text = f"{subject} {snippet} {body}"
    haystack  = full_text.lower()

    # ── Project ID always wins — most definitive signal ───────────────────────
    pid = (project.project_id or "").strip().lower()
    if pid and pid in haystack:
        return True, "project_id", "exact_id"

    # ── Layer 0: noise block — only applies to name-matched emails ────────────
    if _is_noise(subject, sender):
        return False, None, "noise_block"

    # ── Find the strongest name-based signal in the email ─────────────────────
    # Checked in priority order: client_name > calendar_keyword > project_name
    name_signal = None
    for signal_name, val in [
        ("client_name",      project.client_name),
        ("calendar_keyword", project.calendar_keyword),
        ("project_name",     project.name),
    ]:
        # Minimum 4 chars to avoid matching short noise words like "IT" or "AI"
        if val and len(val.strip()) >= 4 and val.strip().lower() in haystack:
            name_signal = signal_name
            break

    if not name_signal:
        return False, None, "no_signal"

    # ── Subject-line strength check ───────────────────────────────────────────
    # A name in the subject line is a strong signal — the sender intentionally
    # titled the email with the project/client name.
    # Exception: bare "Re: [name]" or "Fwd: [name]" with an empty body are
    # just reply thread names — require body content to confirm.
    subject_lower = subject.lower()
    is_bare_reply = (
        re.match(r"^(re|fwd?)\s*:\s*", subject_lower)
        and len((snippet or "").strip()) < 40
    )
    if not is_bare_reply:
        for _, val in [
            ("client_name",      project.client_name),
            ("calendar_keyword", project.calendar_keyword),
            ("project_name",     project.name),
        ]:
            if val and len(val.strip()) >= 4 and val.strip().lower() in subject_lower:
                return True, name_signal, "subject_signal"

    # ── Sender domain check ───────────────────────────────────────────────────
    # If the email came FROM the client's domain, it's almost certainly
    # a project communication e.g. someone@flipkart.com → Flipkart project.
    sender_domain = (sender or "").split("@")[-1].lower().split(".")[0]
    for _, val in [
        ("client_name",      project.client_name),
        ("calendar_keyword", project.calendar_keyword),
    ]:
        if val and len(val.strip()) >= 4:
            clean = val.strip().lower().split()[0]   # first word only to avoid partial matches
            if clean and clean in sender_domain:
                return True, name_signal, "sender_domain"

    # ── Layer 1: keyword gate ─────────────────────────────────────────────────
    if _has_work_signal(full_text):
        return True, name_signal, "keyword_gate"

    # ── Reject project_name-only matches without work signal ──────────────────
    # Project name is the weakest signal — too risky to pass to AI
    # without at least one work keyword confirming it's project-related.
    if name_signal == "project_name":
        return False, None, "project_name_without_work_signal"

    # ── Layer 2: AI gate ──────────────────────────────────────────────────────
    # Only reached for client_name / calendar_keyword matches with no work
    # keywords — genuinely ambiguous. Skip if snippet is too short to judge.
    meaningful_text = (snippet or "").strip()
    if len(meaningful_text) < 40:
        return False, None, "too_short"

    print(f"     🤖 Ambiguous email — asking LLM: \"{subject[:60]}\"")
    ai_result = _ai_is_project_email(
        subject, snippet,
        project.name or project.project_id,
        project.client_name or "",
    )
    reason = "ai_yes" if ai_result else "ai_no"
    return ai_result, (name_signal if ai_result else None), reason


# ══════════════════════════════════════════════════════════════════════════════
# MAIN FETCH
# ══════════════════════════════════════════════════════════════════════════════

def fetch_emails(project):
    """
    Fetch all matching Gmail messages for a project and save new ones to the DB.

    For each message:
      1. Skip if already saved (dedup by gmail_msg_id)
      2. Skip if no Oneture address in From/To/Cc
      3. Run 3-layer relevance check
      4. Classify category + risk signal
      5. Generate summary
      6. Save Email row to DB

    Prints a summary line per email and a final saved/skipped count.
    """
    service    = gmail_service()
    db         = Session()
    new        = 0
    skipped    = 0
    page_token = None

    query = _build_gmail_query(project)
    print(f"\n📧 Fetching emails for {project.project_id} — {project.name}")
    print(f"   Query : {query}")

    try:
        while True:
            kwargs = dict(userId="me", q=query, maxResults=100)
            if page_token:
                kwargs["pageToken"] = page_token

            result     = service.users().messages().list(**kwargs).execute()
            messages   = result.get("messages", [])
            page_token = result.get("nextPageToken")

            for msg in messages:
                # Prefix gmail_msg_id with project ID to allow the same Gmail
                # message to be saved under multiple projects if it matches both
                storage_msg_id = f"{msg['id']}:{project.project_id}"

                # Skip if already saved under either the bare ID or prefixed ID
                if db.query(Email).filter(
                    Email.project_id == project.id,
                    Email.gmail_msg_id.in_([msg["id"], storage_msg_id])
                ).first():
                    continue

                # Fetch full message to get headers, body, and metadata
                detail  = service.users().messages().get(
                    userId="me", id=msg["id"], format="full"
                ).execute()

                headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
                subject = headers.get("Subject", "(no subject)")
                sender  = headers.get("From", "")
                to_raw  = headers.get("To", "")
                cc_raw  = headers.get("Cc", "")
                ts      = int(detail.get("internalDate", 0)) / 1000
                snippet = detail.get("snippet", "")
                body    = _get_body(detail["payload"])
                # Use body for classification if available, else fall back to snippet
                content = body[:500] if body else snippet

                # ── Oneture check — skip emails with no Oneture involvement ───
                if not _has_oneture_address(sender, to_raw, cc_raw):
                    skipped += 1
                    print(f"     ⏭️ Ignored non-Oneture email: {subject[:60]}")
                    continue

                # ── 3-layer relevance check ────────────────────────────────────
                relevant, match_signal, reason = _check_relevance(
                    subject, snippet, body, project, sender
                )

                if not relevant:
                    skipped += 1
                    print(f"     ⛔ Skipped [{reason}]: {subject[:60]}")
                    continue

                # ── Classify + summarise ───────────────────────────────────────
                category, risk = classify_email(subject, content)
                summary        = summarize_email(subject, content, category)

                db.add(Email(
                    project_id      = project.id,
                    gmail_msg_id    = storage_msg_id,
                    gmail_thread_id = detail.get("threadId", ""),
                    subject         = subject,
                    sender          = sender,
                    received_at     = datetime.utcfromtimestamp(ts),
                    category        = category,
                    risk_signal     = risk,
                    snippet         = snippet,
                    summary         = summary,
                    match_signal    = match_signal,
                ))
                new += 1

                risk_tag = "⚠️ " if risk else "  "
                via = f"[via {match_signal}/{reason}]" if match_signal != "project_id" else ""
                print(f"  {risk_tag}[{category:<11}] {subject[:50]:<50} | {sender[:25]} {via}")
                print(f"         → {summary}")

            if not page_token:
                break   # no more pages — all messages fetched

        db.commit()
        print(f"\n  ✅ {new} saved  |  {skipped} skipped — {project.project_id}")

    except Exception as e:
        print(f"  ❌ Gmail error: {e}")
        db.rollback()   # roll back any partial saves on error
    finally:
        db.close()      # always close session even if an exception occurred
