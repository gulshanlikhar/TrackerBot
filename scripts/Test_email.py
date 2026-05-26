"""
test_email.py — Run this directly to diagnose email sending issues.

Usage:
  python test_email.py your@email.com

It will print exactly what is working and what is failing.
"""

import sys, os, base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv
from govtrack.core.paths import ENV_PATH

load_dotenv(ENV_PATH)

def main():
    to_email = sys.argv[1] if len(sys.argv) > 1 else input("Send test email to: ").strip()

    print("\n── Step 1: Load credentials ─────────────────────────")
    try:
        from govtrack.core.google_auth import get_creds, gmail_service
        print("  ✅ google_auth imported OK")
    except Exception as e:
        print(f"  ❌ FAILED to import google_auth: {e}")
        sys.exit(1)

    print("\n── Step 2: Get & refresh token ──────────────────────")
    try:
        from google.auth.transport.requests import Request
        creds = get_creds()
        print(f"  Token valid   : {creds.valid}")
        print(f"  Token expired : {creds.expired}")
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                print("  Refreshing token...")
                creds.refresh(Request())
                print(f"  Token valid after refresh: {creds.valid}")
            else:
                print("  ❌ Token invalid and cannot be refreshed.")
                print("     Delete token.json and re-run google_auth.py to re-authenticate.")
                sys.exit(1)
        print("  ✅ Token OK")
    except Exception as e:
        print(f"  ❌ FAILED: {e}")
        sys.exit(1)

    print("\n── Step 3: Connect to Gmail API ─────────────────────")
    try:
        service = gmail_service()
        profile = service.users().getProfile(userId="me").execute()
        actual_account = profile.get("emailAddress")
        print(f"  ✅ Connected as: {actual_account}")
        print(f"  ℹ️  Emails will be sent FROM this address.")
    except Exception as e:
        print(f"  ❌ FAILED: {e}")
        sys.exit(1)

    print("\n── Step 4: Send test email ───────────────────────────")
    try:
        msg = MIMEMultipart("alternative")
        msg["From"]    = f"GovTrack <{actual_account}>"
        msg["To"]      = to_email
        msg["Subject"] = "[GovTrack] ✅ Test email — sending is working"
        msg.attach(MIMEText(
            "<h2>GovTrack email test</h2><p>If you're reading this, email sending works correctly.</p>",
            "html"
        ))
        raw    = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        result = service.users().messages().send(
            userId="me",
            body={"raw": raw},
        ).execute()
        print(f"  ✅ Email sent! Message id: {result.get('id')}")
        print(f"  ✅ Check inbox at: {to_email}")
    except Exception as e:
        print(f"  ❌ SEND FAILED: {type(e).__name__}: {e}")
        print("\n  Common fixes:")
        print("  • 'insufficient authentication scopes' → delete token.json, re-authenticate")
        print("  • 'invalid_grant' → token expired, delete token.json, re-authenticate")
        print("  • '400 Bad Request' → check GOVTRACK_EMAIL in .env matches your Gmail account")

    # Print what GOVTRACK_EMAIL is set to
    govtrack_email = os.getenv("GOVTRACK_EMAIL", "likharji12@gmail.com")
    print(f"\n── Config summary ───────────────────────────────────")
    print(f"  GOVTRACK_EMAIL (in .env) : {govtrack_email}")
    print(f"  Actual Gmail account     : {actual_account}")
    if govtrack_email.lower() != actual_account.lower():
        print(f"  ⚠️  MISMATCH — update your .env: GOVTRACK_EMAIL={actual_account}")
    else:
        print(f"  ✅ Match — no action needed")

if __name__ == "__main__":
    main()
