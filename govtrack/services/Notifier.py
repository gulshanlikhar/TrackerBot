"""
Notifier.py — Email notification sender for GovTrack.

Sends all outbound emails via the Gmail API using the existing OAuth2 token.
No SMTP server needed — uses Gmail API messages.send directly.

Email types:
  send_pm_confirmation_request()  → Asks PM to confirm auto-detected project via magic link
  send_project_confirmation()     → PM welcome email after confirmation
  send_member_welcome()           → Individual welcome email for each team member
  send_mom_overdue_alert()        → Alert when MoM SLA window is breached
  send_unassigned_email_alert()   → Alert when an email can't be matched to any project
"""

import base64
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv
from govtrack.core.google_auth import get_creds, gmail_service
from google.auth.transport.requests import Request
from govtrack.core.paths import ENV_PATH

load_dotenv(ENV_PATH)

# Gmail account used as the sender for all outbound emails.
# Must exactly match the account in token.json.
# Override with GOVTRACK_EMAIL= in your .env file.
GOVTRACK_EMAIL = os.getenv("GOVTRACK_EMAIL", "likharji12@gmail.com")

def _streamlit_base_url() -> str:
    """Return the public/reachable Streamlit URL used in email links."""
    return (os.getenv("STREAMLIT_BASE_URL") or "http://localhost:8501").strip().rstrip("/")


# ══════════════════════════════════════════════════════════════════════════════
# CORE SEND — GMAIL API
# ══════════════════════════════════════════════════════════════════════════════

def _smtp_send(to: str, cc_list: list, subject: str, html_body: str):
    """
    Send an HTML email via the Gmail API.

    Refreshes the OAuth token before every send to avoid expiry failures.
    Detects and warns if GOVTRACK_EMAIL doesn't match the authenticated account
    — emails are still sent using the actual token account.

    Raises an exception on any failure so callers always see the real error
    rather than silently swallowing it.

    Args:
      to        : recipient email address
      cc_list   : list of CC email addresses (can be empty)
      subject   : email subject line
      html_body : full HTML string for the email body
    """
    # Refresh token if expired — prevents stale token errors on long-running instances
    creds = get_creds()
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError(
                "Gmail credentials are invalid and cannot be refreshed. "
                "Delete token.json and re-authenticate by running: python google_auth.py"
            )

    # Verify which Gmail account the token actually belongs to
    service = gmail_service()
    try:
        profile        = service.users().getProfile(userId="me").execute()
        actual_account = profile.get("emailAddress", "unknown")
    except Exception:
        actual_account = "unknown"

    # Warn if .env GOVTRACK_EMAIL doesn't match the token account
    # (common misconfiguration — emails still send but from the wrong address)
    if actual_account.lower() != GOVTRACK_EMAIL.lower():
        print(f"  ⚠️  GOVTRACK_EMAIL is '{GOVTRACK_EMAIL}' but token belongs to '{actual_account}'.")
        print(f"  ⚠️  Sending from '{actual_account}'. Update GOVTRACK_EMAIL in .env to suppress this.")
        sender = actual_account
    else:
        sender = GOVTRACK_EMAIL

    # Build the MIME message
    msg            = MIMEMultipart("alternative")
    msg["From"]    = f"GovTrack <{sender}>"
    msg["To"]      = to
    msg["Subject"] = subject
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg.attach(MIMEText(html_body, "html"))

    # Gmail API requires the message encoded as base64url
    raw    = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    result = service.users().messages().send(
        userId="me",
        body={"raw": raw},
    ).execute()

    print(
        f"  ✅ Email sent from {sender} → {to} (msg id: {result.get('id', '?')})"
        + (f"  CC: {cc_list}" if cc_list else "")
    )


# ══════════════════════════════════════════════════════════════════════════════
# PM CONFIRMATION REQUEST
# ══════════════════════════════════════════════════════════════════════════════

def send_pm_confirmation_request(project, pm_email: str, member_emails: list = None):
    """
    Sent when GovTrack auto-detects a new project from an incoming email.

    Asks the PM to click a magic link and verify/edit all auto-detected details.
    The link opens a Streamlit confirmation page pre-filled with editable fields.

    Token-based magic link: each project gets a unique pm_confirm_token
    so only the intended PM can confirm their project.

    Members are NOT CC'd on this email — it's a PM-only action request.
    """
    # Build magic link using the one-time token stored on the project
    confirm_url = f"{_streamlit_base_url()}/?confirm_token={project.pm_confirm_token}"

    start   = project.start_date.strftime("%d %b %Y")   if project.start_date   else "—"
    go_live = project.go_live_date.strftime("%d %b %Y") if project.go_live_date else "—"

    # Build HTML table rows for detected team members (shown in Q3 section)
    member_rows = ""
    if member_emails:
        for email in member_emails:
            member_rows += (
                f"<tr>"
                f"<td style='padding:6px 12px;border-bottom:1px solid #E5E4DE'>{email}</td>"
                f"<td style='padding:6px 12px;border-bottom:1px solid #E5E4DE;color:#6C6B63'>"
                f"Team Member</td></tr>"
            )

    html = f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#F5F5F0;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#F5F5F0;padding:32px 0">
<tr><td align="center">
<table width="620" cellpadding="0" cellspacing="0"
       style="background:#FFFFFF;border-radius:12px;border:1px solid #E5E4DE;overflow:hidden">

  <!-- Header -->
  <tr><td style="background:#1A1A18;padding:24px 32px">
    <div style="font-size:20px;font-weight:700;color:#FFFFFF">🛡️ GovTrack</div>
    <div style="font-size:12px;color:#8A8980;margin-top:4px">
      Delivery Governance Platform · Oneture Technologies
    </div>
  </td></tr>

  <!-- Body -->
  <tr><td style="padding:32px 32px 24px">

    <div style="font-size:18px;font-weight:600;color:#1A1A18;margin-bottom:6px">
      New Project Detected — Please Confirm Details
    </div>
    <div style="font-size:13px;color:#6C6B63;line-height:1.7;margin-bottom:24px">
      GovTrack has auto-detected a new project from your email with subject
      <b>{project.project_id} | {project.name}</b>.<br>
      We've pre-filled the details below based on your email. Please review each section,
      make any corrections needed, and submit your confirmation.
    </div>

    <!-- Primary CTA -->
    <div style="text-align:center;margin-bottom:28px">
      <a href="{confirm_url}"
         style="display:inline-block;background:#5C3FD4;color:#FFFFFF;font-size:14px;
                font-weight:600;padding:14px 36px;border-radius:8px;text-decoration:none;
                letter-spacing:.02em">
        ✏️ &nbsp; Review &amp; Confirm Project Details
      </a>
      <div style="font-size:11px;color:#8A8980;margin-top:8px">
        This link is unique to you and will open a secure confirmation form.
      </div>
    </div>

    <div style="border-top:1px solid #E5E4DE;padding-top:20px;margin-bottom:4px">
      <div style="font-size:12px;font-weight:600;color:#8A8980;letter-spacing:.06em;
                  text-transform:uppercase;margin-bottom:12px">What We Auto-Detected</div>
    </div>

    <!-- Q1: PM confirmation -->
    <div style="background:#F9F9F6;border:1px solid #E5E4DE;border-radius:8px;
                padding:14px 16px;margin-bottom:10px">
      <div style="font-size:11px;font-weight:600;color:#5C3FD4;text-transform:uppercase;
                  letter-spacing:.06em;margin-bottom:8px">
        ❓ Question 1 — Are you the Project Manager?
      </div>
      <table style="font-size:13px;border-collapse:collapse;width:100%">
        <tr>
          <td style="color:#8A8980;width:130px;padding:3px 0">Your email</td>
          <td style="color:#1A1A18;font-weight:500">{pm_email}</td>
        </tr>
        <tr>
          <td style="color:#8A8980;padding:3px 0">Delivery Lead</td>
          <td style="color:#1A1A18;font-weight:500">{project.delivery_lead or "—"}</td>
        </tr>
      </table>
      <div style="font-size:12px;color:#5C3FD4;margin-top:8px">
        👉 Click the button above to confirm or update your name/role.
      </div>
    </div>

    <!-- Q2: Client name -->
    <div style="background:#F9F9F6;border:1px solid #E5E4DE;border-radius:8px;
                padding:14px 16px;margin-bottom:10px">
      <div style="font-size:11px;font-weight:600;color:#5C3FD4;text-transform:uppercase;
                  letter-spacing:.06em;margin-bottom:8px">
        ❓ Question 2 — Is this the correct client?
      </div>
      <table style="font-size:13px;border-collapse:collapse;width:100%">
        <tr>
          <td style="color:#8A8980;width:130px;padding:3px 0">Client Name</td>
          <td style="color:#1A1A18;font-weight:500">{project.client_name or "—"}</td>
        </tr>
        <tr>
          <td style="color:#8A8980;padding:3px 0">Detected from</td>
          <td style="color:#6C6B63">Email "To" field</td>
        </tr>
      </table>
      <div style="font-size:12px;color:#5C3FD4;margin-top:8px">
        👉 Click the button above to confirm or update the client name and email.
      </div>
    </div>

    <!-- Q3: Team members -->
    <div style="background:#F9F9F6;border:1px solid #E5E4DE;border-radius:8px;
                padding:14px 16px;margin-bottom:10px">
      <div style="font-size:11px;font-weight:600;color:#5C3FD4;text-transform:uppercase;
                  letter-spacing:.06em;margin-bottom:8px">
        ❓ Question 3 — Are these the correct team members?
      </div>
      <div style="font-size:12px;color:#6C6B63;margin-bottom:8px">
        Detected from the "Cc" field of your email:
      </div>
      {
        '<table style="font-size:12px;border-collapse:collapse;width:100%;'
        'border:1px solid #E5E4DE;border-radius:6px;overflow:hidden">'
        '<thead><tr style="background:#EEEDE8">'
        '<th style="padding:6px 12px;text-align:left;color:#8A8980">Email</th>'
        '<th style="padding:6px 12px;text-align:left;color:#8A8980">Role</th>'
        '</tr></thead><tbody>' + member_rows + '</tbody></table>'
        if member_rows
        else '<div style="font-size:12px;color:#8A8980">No Cc recipients detected.</div>'
      }
      <div style="font-size:12px;color:#5C3FD4;margin-top:8px">
        👉 Click the button above to add, remove, or edit team members.
      </div>
    </div>

    <!-- Q4: Full project statement -->
    <div style="background:#F9F9F6;border:1px solid #E5E4DE;border-radius:8px;
                padding:14px 16px;margin-bottom:24px">
      <div style="font-size:11px;font-weight:600;color:#5C3FD4;text-transform:uppercase;
                  letter-spacing:.06em;margin-bottom:8px">
        ❓ Question 4 — Is this project statement correct?
      </div>
      <table style="font-size:13px;border-collapse:collapse;width:100%">
        <tr><td style="color:#8A8980;width:130px;padding:3px 0">Project ID</td>
            <td style="color:#1A1A18;font-weight:500">{project.project_id}</td></tr>
        <tr><td style="color:#8A8980;padding:3px 0">Project Name</td>
            <td style="color:#1A1A18;font-weight:500">{project.name}</td></tr>
        <tr><td style="color:#8A8980;padding:3px 0">Client</td>
            <td style="color:#1A1A18;font-weight:500">{project.client_name or "—"}</td></tr>
        <tr><td style="color:#8A8980;padding:3px 0">Engagement</td>
            <td style="color:#1A1A18;font-weight:500">{project.engagement or "—"}</td></tr>
        <tr><td style="color:#8A8980;padding:3px 0">Start Date</td>
            <td style="color:#1A1A18;font-weight:500">{start}</td></tr>
        <tr><td style="color:#8A8980;padding:3px 0">Go-Live Date</td>
            <td style="color:#1A1A18;font-weight:500">{go_live}</td></tr>
        <tr><td style="color:#8A8980;padding:3px 0">Description</td>
            <td style="color:#1A1A18;font-weight:500">{project.description or "—"}</td></tr>
      </table>
      <div style="font-size:12px;color:#5C3FD4;margin-top:8px">
        👉 Click the button above to correct any of these fields.
      </div>
    </div>

    <!-- Secondary CTA -->
    <div style="text-align:center;margin-bottom:24px">
      <a href="{confirm_url}"
         style="display:inline-block;background:#5C3FD4;color:#FFFFFF;font-size:13px;
                font-weight:600;padding:12px 32px;border-radius:8px;text-decoration:none">
        ✏️ &nbsp; Open Confirmation Form
      </a>
    </div>

    <div style="font-size:12px;color:#8A8980;border-top:1px solid #E5E4DE;
                padding-top:16px;line-height:1.7">
      This is an automated message from GovTrack — Oneture Technologies.<br>
      If you did not send a project-related email, please ignore this message.<br>
      Do not reply to this email. Use the confirmation link above.
    </div>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""

    _smtp_send(
        to        = pm_email,
        cc_list   = [],   # PM-only — members must not see the magic link
        subject   = (
            f"[GovTrack] Action Required — Confirm Your Project: "
            f"{project.project_id} | {project.name}"
        ),
        html_body = html,
    )


# ══════════════════════════════════════════════════════════════════════════════
# PM WELCOME (POST-CONFIRMATION)
# ══════════════════════════════════════════════════════════════════════════════

def send_project_confirmation(project, pm_email: str, member_emails: list = None):
    """
    Sent TO the PM only after they confirm the project.
    Also triggers individual welcome emails to each team member.

    PM is NOT CC'd on member welcome emails — each person gets their own
    separate email to avoid exposing the full team list.
    """
    start   = project.start_date.strftime("%d %b %Y")   if project.start_date   else "—"
    go_live = project.go_live_date.strftime("%d %b %Y") if project.go_live_date else "—"

    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#F5F5F0;font-family:sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#F5F5F0;padding:32px 0">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0"
       style="background:#FFFFFF;border-radius:12px;border:1px solid #E5E4DE;overflow:hidden">

  <!-- Header -->
  <tr><td style="background:#1A1A18;padding:24px 32px">
    <div style="font-size:20px;font-weight:700;color:#FFFFFF">🛡️ GovTrack</div>
    <div style="font-size:12px;color:#8A8980;margin-top:4px">
      Delivery Governance Platform · Oneture Technologies
    </div>
  </td></tr>

  <!-- Body -->
  <tr><td style="padding:32px">
    <div style="font-size:18px;font-weight:600;color:#1A1A18;margin-bottom:6px">
      You are the Project Manager for
      <span style="color:#5C3FD4">{project.name}</span>
    </div>
    <div style="font-size:13px;color:#6C6B63;margin-bottom:24px">
      Your project is now active in GovTrack. Team members have been notified separately.
    </div>

    <!-- Project details table -->
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #E5E4DE;border-radius:8px;overflow:hidden;
                  font-size:13px;margin-bottom:24px">
      <tr style="background:#F5F5F0">
        <td style="padding:10px 16px;color:#8A8980;font-weight:500;width:38%">Project ID</td>
        <td style="padding:10px 16px;color:#1A1A18;font-weight:600">{project.project_id}</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;color:#8A8980;border-top:1px solid #E5E4DE">Project Name</td>
        <td style="padding:10px 16px;color:#1A1A18;border-top:1px solid #E5E4DE">{project.name}</td>
      </tr>
      <tr style="background:#F5F5F0">
        <td style="padding:10px 16px;color:#8A8980">Client</td>
        <td style="padding:10px 16px;color:#1A1A18">{project.client_name}</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;color:#8A8980;border-top:1px solid #E5E4DE">Engagement</td>
        <td style="padding:10px 16px;color:#1A1A18;border-top:1px solid #E5E4DE">{project.engagement}</td>
      </tr>
      <tr style="background:#F5F5F0">
        <td style="padding:10px 16px;color:#8A8980">Start Date</td>
        <td style="padding:10px 16px;color:#1A1A18">{start}</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;color:#8A8980;border-top:1px solid #E5E4DE">Go-Live Date</td>
        <td style="padding:10px 16px;color:#1A1A18;border-top:1px solid #E5E4DE">{go_live}</td>
      </tr>
      <tr style="background:#F5F5F0">
        <td style="padding:10px 16px;color:#8A8980">Delivery %</td>
        <td style="padding:10px 16px;color:#1A1A18">{project.delivery_pct}%</td>
      </tr>
    </table>

    <div style="font-size:12px;color:#8A8980;border-top:1px solid #E5E4DE;padding-top:16px">
      Automated message from GovTrack · Oneture Technologies. Do not reply.
    </div>
  </td></tr>
</table></td></tr></table>
</body></html>"""

    _smtp_send(
        to        = pm_email,
        cc_list   = [],
        subject   = f"[GovTrack] ✅ You are the PM for {project.project_id} — {project.name}",
        html_body = html,
    )

    # Send individual welcome emails to each team member
    # Skips the PM themselves and any blank/invalid addresses
    for email in (member_emails or []):
        if email and email.strip() and email.strip().lower() != pm_email.strip().lower():
            try:
                send_member_welcome(project, email.strip())
            except Exception as e:
                print(f"  ⚠️  Member welcome to {email} failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# MEMBER WELCOME
# ══════════════════════════════════════════════════════════════════════════════

def send_member_welcome(project, member_email: str):
    """
    Sent individually to each team member when they are added to a project.

    Intentionally says "you've been added" (not "you are the PM") to keep
    messaging accurate for non-PM members.
    Each member gets a separate email — no group CC — for privacy.
    """
    start   = project.start_date.strftime("%d %b %Y")   if project.start_date   else "—"
    go_live = project.go_live_date.strftime("%d %b %Y") if project.go_live_date else "—"
    # Use delivery_lead name if available, fall back to PM email, then generic label
    pm_name = project.delivery_lead or project.pm_email or "your Project Manager"

    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#F5F5F0;font-family:sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#F5F5F0;padding:32px 0">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0"
       style="background:#FFFFFF;border-radius:12px;border:1px solid #E5E4DE;overflow:hidden">

  <!-- Header -->
  <tr><td style="background:#1A1A18;padding:24px 32px">
    <div style="font-size:20px;font-weight:700;color:#FFFFFF">🛡️ GovTrack</div>
    <div style="font-size:12px;color:#8A8980;margin-top:4px">
      Delivery Governance Platform · Oneture Technologies
    </div>
  </td></tr>

  <!-- Body -->
  <tr><td style="padding:32px">
    <div style="font-size:18px;font-weight:600;color:#1A1A18;margin-bottom:6px">
      You've been added to <span style="color:#5C3FD4">{project.name}</span>
    </div>
    <div style="font-size:13px;color:#6C6B63;margin-bottom:24px">
      You have been added as a team member on this project in GovTrack.
      Your Project Manager is <b>{pm_name}</b>.
    </div>

    <!-- Project details table -->
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #E5E4DE;border-radius:8px;overflow:hidden;
                  font-size:13px;margin-bottom:24px">
      <tr style="background:#F5F5F0">
        <td style="padding:10px 16px;color:#8A8980;font-weight:500;width:38%">Project ID</td>
        <td style="padding:10px 16px;color:#1A1A18;font-weight:600">{project.project_id}</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;color:#8A8980;border-top:1px solid #E5E4DE">Project Name</td>
        <td style="padding:10px 16px;color:#1A1A18;border-top:1px solid #E5E4DE">{project.name}</td>
      </tr>
      <tr style="background:#F5F5F0">
        <td style="padding:10px 16px;color:#8A8980">Client</td>
        <td style="padding:10px 16px;color:#1A1A18">{project.client_name}</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;color:#8A8980;border-top:1px solid #E5E4DE">Start Date</td>
        <td style="padding:10px 16px;color:#1A1A18;border-top:1px solid #E5E4DE">{start}</td>
      </tr>
      <tr style="background:#F5F5F0">
        <td style="padding:10px 16px;color:#8A8980">Go-Live Date</td>
        <td style="padding:10px 16px;color:#1A1A18">{go_live}</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;color:#8A8980;border-top:1px solid #E5E4DE">Project Manager</td>
        <td style="padding:10px 16px;color:#1A1A18;border-top:1px solid #E5E4DE">{pm_name}</td>
      </tr>
    </table>

    <div style="font-size:12px;color:#8A8980;border-top:1px solid #E5E4DE;padding-top:16px">
      Automated message from GovTrack · Oneture Technologies. Do not reply.
    </div>
  </td></tr>
</table></td></tr></table>
</body></html>"""

    _smtp_send(
        to        = member_email,
        cc_list   = [],
        subject   = f"[GovTrack] You've been added to {project.project_id} — {project.name}",
        html_body = html,
    )


# ══════════════════════════════════════════════════════════════════════════════
# MOM OVERDUE ALERT
# ══════════════════════════════════════════════════════════════════════════════

def send_mom_overdue_alert(project, meeting, pm_email: str, hours_overdue: float):
    """
    Sent to the PM when the MoM SLA window has been breached after a meeting.
    Triggered by the Email_watcher when mom_deadline has passed and mom_sent=False.

    Args:
      project       : Project ORM object
      meeting       : Meeting ORM object (the overdue meeting)
      pm_email      : PM's email address to send the alert to
      hours_overdue : how many hours past the SLA deadline (shown in the email)
    """
    meeting_dt = meeting.meeting_date.strftime("%d %b %Y %H:%M")

    html = f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#F5F5F0;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#F5F5F0;padding:32px 0">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0"
       style="background:#FFFFFF;border-radius:12px;border:1px solid #E5E4DE;overflow:hidden">

  <!-- Red header signals high urgency -->
  <tr><td style="background:#E53E3E;padding:24px 32px">
    <div style="font-size:20px;font-weight:700;color:#FFFFFF">🔴 GovTrack — MoM SLA Breached</div>
    <div style="font-size:12px;color:#FECACA;margin-top:4px">
      {project.project_id} · {project.name}
    </div>
  </td></tr>

  <tr><td style="padding:32px">
    <div style="font-size:16px;font-weight:600;color:#1A1A18;margin-bottom:12px">
      Minutes of Meeting not sent — SLA exceeded
    </div>

    <!-- Meeting details in red-tinted box to draw attention -->
    <div style="background:#FCE4E4;border:1px solid #FCA5A5;border-radius:8px;
                padding:16px 20px;margin-bottom:20px">
      <div style="font-size:13px;color:#8B1A1A;line-height:1.7">
        <b>Meeting:</b> {meeting.title}<br>
        <b>Date:</b> {meeting_dt}<br>
        <b>Attendees:</b> {meeting.attendees}<br>
        <b>Overdue by:</b> {hours_overdue:.1f} hours
      </div>
    </div>

    <div style="font-size:13px;color:#6C6B63;line-height:1.6;margin-bottom:20px">
      The Minutes of Meeting for the above session have
      <b>not been marked as sent</b> within the required SLA window.
      Please send the MoM immediately and update the status in the GovTrack Dashboard.
    </div>

    <div style="font-size:12px;color:#8A8980;border-top:1px solid #E5E4DE;padding-top:16px">
      Automated governance alert from GovTrack · Oneture Technologies
    </div>
  </td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""

    _smtp_send(
        to        = pm_email,
        cc_list   = [],
        subject   = (
            f"[GovTrack] 🔴 MoM Overdue — {meeting.title} | "
            f"{project.project_id}"
        ),
        html_body = html,
    )


# ══════════════════════════════════════════════════════════════════════════════
# UNASSIGNED EMAIL ALERT
# ══════════════════════════════════════════════════════════════════════════════

def send_unassigned_email_alert(unmapped_email, sender_email: str):
    """
    Sent to the Oneture sender when their email could not be matched to any project.

    Provides two resolution options:
      Option 1 — Reply to the original email with the PRJ-XXXX ID so GovTrack
                 auto-maps it on the next scan cycle.
      Option 2 — Create a new project in the GovTrack dashboard via a deep link.

    Args:
      unmapped_email : UnmappedEmail ORM object
      sender_email   : Oneture email address to send the alert to
    """
    # Deep link to the Add Project page in the Streamlit dashboard
    add_url  = f"{_streamlit_base_url()}/?page=Add+Project"
    received = (
        unmapped_email.received_at.strftime("%d %b %Y  %H:%M")
        if unmapped_email.received_at else "—"
    )
    # Show a preview of the email body so the recipient can identify it
    preview = (unmapped_email.body_preview or unmapped_email.snippet or "—")[:300]

    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#F5F5F0;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#F5F5F0;padding:32px 0">
<tr><td align="center">
<table width="620" cellpadding="0" cellspacing="0"
       style="background:#FFFFFF;border-radius:12px;border:1px solid #E5E4DE;overflow:hidden">

  <!-- Orange header — warning level, not critical -->
  <tr><td style="background:#F6A623;padding:24px 32px">
    <div style="font-size:20px;font-weight:700;color:#FFFFFF">
      🟠 GovTrack — Project Not Found
    </div>
    <div style="font-size:12px;color:#FFF8EC;margin-top:4px">
      An email thread could not be linked to any active project.
    </div>
  </td></tr>

  <!-- Body -->
  <tr><td style="padding:32px 32px 24px">
    <div style="font-size:16px;font-weight:600;color:#1A1A18;margin-bottom:8px">
      Please claim or create this project
    </div>
    <div style="font-size:13px;color:#6C6B63;line-height:1.7;margin-bottom:22px">
      GovTrack received an email that looks like a project communication, but it
      <b>does not include a project ID (PRJ-XXXX)</b> and could not be matched to any
      existing project. To keep governance tracking active, please take one of the
      two actions below.
    </div>

    <!-- The unmatched email details -->
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #E5E4DE;border-radius:8px;overflow:hidden;
                  font-size:13px;margin-bottom:24px">
      <tr style="background:#F5F5F0">
        <td style="padding:10px 16px;color:#8A8980;font-weight:500;width:110px">Subject</td>
        <td style="padding:10px 16px;color:#1A1A18;font-weight:600">
          {unmapped_email.subject or "(no subject)"}
        </td>
      </tr>
      <tr>
        <td style="padding:10px 16px;color:#8A8980;border-top:1px solid #E5E4DE">From</td>
        <td style="padding:10px 16px;color:#1A1A18;border-top:1px solid #E5E4DE">
          {unmapped_email.sender or "—"}
        </td>
      </tr>
      <tr style="background:#F5F5F0">
        <td style="padding:10px 16px;color:#8A8980">Received</td>
        <td style="padding:10px 16px;color:#1A1A18">{received}</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;color:#8A8980;border-top:1px solid #E5E4DE;
                   vertical-align:top">Preview</td>
        <td style="padding:10px 16px;color:#3A3A34;border-top:1px solid #E5E4DE;
                   font-size:12px;line-height:1.6;font-style:italic">
          {preview}
        </td>
      </tr>
    </table>

    <div style="font-size:12px;font-weight:600;color:#8A8980;text-transform:uppercase;
                letter-spacing:.06em;margin-bottom:12px">What to do</div>

    <!-- Option 1: Reply with PRJ-ID -->
    <div style="background:#F9F9F6;border:1px solid #E5E4DE;border-radius:8px;
                padding:16px 18px;margin-bottom:10px">
      <div style="font-size:13px;font-weight:600;color:#1A1A18;margin-bottom:4px">
        Option 1 — Reply with the Project ID
      </div>
      <div style="font-size:13px;color:#6C6B63;line-height:1.6">
        If this email belongs to an existing project, simply reply to the original email
        and include the project ID in the format <b>PRJ-XXXX</b> anywhere in the subject or body.
        GovTrack will auto-map it on the next scan.
      </div>
    </div>

    <!-- Option 2: Create new project -->
    <div style="background:#F9F9F6;border:1px solid #E5E4DE;border-radius:8px;
                padding:16px 18px;margin-bottom:24px">
      <div style="font-size:13px;font-weight:600;color:#1A1A18;margin-bottom:4px">
        Option 2 — Create a new project in GovTrack
      </div>
      <div style="font-size:13px;color:#6C6B63;line-height:1.6;margin-bottom:12px">
        If this is a new engagement, add it as a project in the dashboard. Once created,
        future emails mentioning its ID or client name will be tracked automatically.
      </div>
      <a href="{add_url}"
         style="display:inline-block;background:#5C3FD4;color:#FFFFFF;font-size:13px;
                font-weight:600;padding:11px 28px;border-radius:8px;text-decoration:none">
        ➕ &nbsp; Add Project in GovTrack
      </a>
    </div>

    <div style="font-size:12px;color:#8A8980;border-top:1px solid #E5E4DE;padding-top:16px">
      Automated governance alert from GovTrack · Oneture Technologies.
      Do not reply directly to this message.
    </div>
  </td></tr>
</table></td></tr></table>
</body></html>"""

    _smtp_send(
        to        = sender_email,
        cc_list   = [],
        subject   = (
            f"[GovTrack] 🟠 Untracked email — please claim or create a project | "
            f"{(unmapped_email.subject or 'No subject')[:50]}"
        ),
        html_body = html,
    )
