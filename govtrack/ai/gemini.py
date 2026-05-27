"""
gemini.py — Rule-based AI replacement for GovTrack.

Originally designed to call the Gemini API, this module now uses
keyword matching instead — no API key or internet connection needed.

Provides three capabilities:
  1. classify_email()    → category + risk flag from subject/snippet
  2. summarize_email()   → one-line summary with risk flags highlighted
  3. summarize_meeting() → one-line meeting context string
  4. generate_alerts()   → governance alert blocks from emails + meetings
"""

import re


# ══════════════════════════════════════════════════════════════════════════════
# KEYWORD LISTS
# ══════════════════════════════════════════════════════════════════════════════

# Triggers email category: MoM (Minutes of Meeting)
MOM_KEYWORDS = [
    "minutes", "mom", "action items",
    "meeting notes", "recap", "decisions taken"
]

# Triggers email category: WBR (Weekly Business Review)
WBR_KEYWORDS = [
    "wbr", "weekly business review", "weekly status",
    "weekly report", "status report"
]

MOM_CONTEXT_SIGNALS = [
    "attendees", "participants", "agenda", "discussion points",
    "key discussion", "decisions", "decision taken", "decisions taken",
    "action owner", "owner", "due date", "next steps", "open items",
    "meeting summary", "call summary", "call notes", "follow up items",
]

WBR_CONTEXT_SIGNALS = [
    "week ending", "this week", "last week", "next week",
    "weekly update", "progress update", "accomplishments", "completed this week",
    "planned for next week", "plan for next week", "risks and issues",
    "status summary", "health status", "milestone status", "kpi",
]

# Triggers email category: Delay — also contributes to risk_signal=True
DELAY_KEYWORDS = [
    "delay", "delayed", "postpone", "pushed", "at risk",
    "go-live risk", "timeline risk", "blocker", "blocked",
    "uat pushed", "behind schedule", "slippage"
]

# Triggers email category: Escalation — highest severity, always risk_signal=True
ESCALATION_KEYWORDS = [
    "escalation", "escalate", "cxo", "cto", "ceo",
    "complaint", "formal concern", "dissatisfied",
    "unhappy", "urgent concern", "critical issue"
]

# Combined risk list used for risk_signal detection and alert generation.
# Includes delay + escalation keywords plus additional general risk terms.
RISK_KEYWORDS = DELAY_KEYWORDS + ESCALATION_KEYWORDS + [
    "concern", "failure", "failed", "overdue",
    "breach", "missed", "critical", "not delivered"
]


# ══════════════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _text(subject: str, snippet: str) -> str:
    """
    Combine subject and snippet into a single lowercase string
    for keyword matching. Lowercasing ensures case-insensitive detection.
    """
    return (subject + " " + snippet).lower()


def _count_context_hits(text: str, signals: list[str]) -> int:
    """Count distinct context phrases present in the email text."""
    return sum(1 for signal in signals if signal in text)


def _looks_like_structured_mom(text: str) -> bool:
    """Detect MoM by meeting-note structure, not only by the word MoM."""
    if _count_context_hits(text, MOM_CONTEXT_SIGNALS) >= 2:
        return True
    return bool(
        re.search(r"\b(action\s+items?|decisions?|next\s+steps)\b", text)
        and re.search(r"\b(meeting|call|discussion|review)\b", text)
    )


def _looks_like_structured_wbr(text: str) -> bool:
    """Detect WBR by weekly-report structure, not only by the word WBR."""
    if _count_context_hits(text, WBR_CONTEXT_SIGNALS) >= 2:
        return True
    return bool(
        re.search(r"\b(week|weekly)\b", text)
        and re.search(r"\b(progress|status|risks?|issues?|next\s+week|milestones?)\b", text)
    )


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL CLASSIFICATION
# ══════════════════════════════════════════════════════════════════════════════

def classify_email(subject: str, snippet: str) -> tuple[str, bool]:
    """
    Classify an email into a category and determine if it carries a risk signal.

    Priority order (highest to lowest):
      Escalation → Delay → MoM → WBR → General

    Returns:
      (category: str, risk_signal: bool)
      e.g. ("Escalation", True) or ("MoM", False)
    """
    t = _text(subject, snippet)

    # Escalation is highest priority — always a risk signal
    if any(k in t for k in ESCALATION_KEYWORDS):
        return "Escalation", True

    # Delay signals — project timeline at risk
    if any(k in t for k in DELAY_KEYWORDS):
        return "Delay", True

    # MoM and WBR are informational — not inherently risky
    if any(k in t for k in MOM_KEYWORDS) or _looks_like_structured_mom(t):
        return "MoM", False

    if any(k in t for k in WBR_KEYWORDS) or _looks_like_structured_wbr(t):
        return "WBR", False

    # General email — still check for any risk keywords in the broader list
    risk = any(k in t for k in RISK_KEYWORDS)
    return "General", risk


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL SUMMARISATION
# ══════════════════════════════════════════════════════════════════════════════

def summarize_email(subject: str, snippet: str, category: str) -> str:
    """
    Build a one-line summary for an email, shown in the dashboard and terminal.

    Format:
      [Category] Subject — Risk flags: keyword1, keyword2   (if risk found)
      [Category] Subject — first 100 chars of snippet       (if no risk)

    Limits risk flags to 3 to keep the summary concise.
    """
    t = _text(subject, snippet)

    # Collect which specific risk keywords triggered in this email
    risk_flags = [k for k in RISK_KEYWORDS if k in t]

    base = f"[{category}] {subject.strip()}"

    if risk_flags:
        # Show up to 3 risk keywords so the summary stays readable
        base += f" — Risk flags: {', '.join(risk_flags[:3])}"
    elif snippet:
        # No risk — append a cleaned snippet preview (max 100 chars)
        clean = snippet.strip().replace("\n", " ")[:100]
        base += f" — {clean}"

    return base


# ══════════════════════════════════════════════════════════════════════════════
# MEETING SUMMARISATION
# ══════════════════════════════════════════════════════════════════════════════

def summarize_meeting(title: str, date: str, attendees: int) -> str:
    """
    Generate a one-line context string for a calendar meeting.
    Used as the meeting summary stored in the DB and shown on the dashboard.

    Infers meeting type from the title using keyword matching.
    Falls back to "Project meeting" if no keywords match.
    """
    t = title.lower()

    if any(k in t for k in ["wbr", "weekly business", "status"]):
        mtype = "Weekly business review"
    elif any(k in t for k in ["mom", "minutes", "follow"]):
        mtype = "Follow-up / minutes review"
    elif any(k in t for k in ["kickoff", "kick-off", "kick off"]):
        mtype = "Project kickoff meeting"
    else:
        mtype = "Project meeting"

    return f"{mtype} on {date} with {attendees} attendees — check for MoM and action items."


# ══════════════════════════════════════════════════════════════════════════════
# GOVERNANCE ALERT GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def generate_alerts(
    project_name: str,
    client: str,
    emails_summary: str,
    meetings_summary: str,
    gov_rules: str,
) -> str:
    """
    Analyse a project's email and meeting summaries against governance rules
    and return a formatted string of alert blocks.

    Each alert block follows this format (parsed by alerts.py):
        ALERT [RED|ORANGE|GREEN]
        Title: ...
        Description: ...
        ---

    Alert levels:
      RED    → immediate action needed (escalation, schedule risk)
      ORANGE → warning (missing MoM, no WBR, no data synced)
      GREEN  → all checks passing

    Note: project_name, client, and gov_rules are accepted as parameters
    for future use (e.g. passing to a real AI API) but not used in the
    current keyword-based implementation.
    """
    alerts = []

    # Combine emails and meetings into one searchable text block
    text = (emails_summary + " " + meetings_summary).lower()

    # ── RED: Schedule / delay risk ────────────────────────────────────────────
    delay_hits = [k for k in DELAY_KEYWORDS if k in text]
    if delay_hits:
        alerts.append(
            "ALERT [RED]\nTitle: Schedule Risk Detected\n"
            f"Description: Emails contain delay/risk signals: {', '.join(delay_hits[:4])}.\n---"
        )

    # ── RED: Client escalation ────────────────────────────────────────────────
    esc_hits = [k for k in ESCALATION_KEYWORDS if k in text]
    if esc_hits:
        alerts.append(
            "ALERT [RED]\nTitle: Client Escalation Signal\n"
            f"Description: Escalation keywords found in communications: {', '.join(esc_hits[:3])}.\n---"
        )

    # ── ORANGE: MoM not sent for one or more meetings ─────────────────────────
    # Checks three possible string formats that calendar_reader may produce
    if "mom sent: no" in text or "mom:❌" in text or "mom sent: false" in text:
        alerts.append(
            "ALERT [ORANGE]\nTitle: MoM Not Sent\n"
            "Description: One or more meetings have no Minutes of Meeting recorded"
            " — violates MOM_SLA rule.\n---"
        )

    # ── ORANGE: No emails synced yet ──────────────────────────────────────────
    if "no emails fetched" in text:
        alerts.append(
            "ALERT [ORANGE]\nTitle: No Emails Synced\n"
            "Description: No project emails have been fetched yet"
            " — run sync to pull latest communications.\n---"
        )

    # ── ORANGE: No meetings synced yet ────────────────────────────────────────
    if "no meetings fetched" in text:
        alerts.append(
            "ALERT [ORANGE]\nTitle: No Meetings Synced\n"
            "Description: No calendar meetings found"
            " — verify calendar keyword and run sync.\n---"
        )

    # ── ORANGE: No WBR email found this period ────────────────────────────────
    if "wbr" not in text and "weekly business review" not in text:
        alerts.append(
            "ALERT [ORANGE]\nTitle: No WBR Emails Found\n"
            "Description: No Weekly Business Review emails detected"
            " — WBR_SLA may be at risk.\n---"
        )

    # ── GREEN: Everything looks fine ─────────────────────────────────────────
    # Only added if no RED or ORANGE alerts were raised above
    if not alerts:
        alerts.append(
            "ALERT [GREEN]\nTitle: All Checks Passing\n"
            "Description: No governance issues detected based on current emails and meetings.\n---"
        )

    # Each alert block is separated by a newline; alerts.py splits on "---"
    return "\n".join(alerts)
