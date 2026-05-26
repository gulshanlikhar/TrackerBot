"""
import_pdf.py — PDF-based project importer for GovTrack.

Extracts project details and governance rules from a PDF brief
using regex field parsing. No AI or internet connection needed.

Usage (CLI):
  python main.py import-pdf <path_to_pdf>

Usage (Streamlit):
  from govtrack.integrations.import_pdf import extract_from_pdf, import_pdf
"""

import os
import sys
import re
from datetime import datetime
from govtrack.core.models import Session, Project, GovernanceRule

# Try pypdf first (modern), fall back to PyPDF2 (legacy).
# Both expose the same PdfReader interface so no other code changes needed.
try:
    from pypdf import PdfReader
except ImportError:
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        print("❌ Missing dependency. Run: pip install pypdf")
        sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
# DEFAULTS
# ══════════════════════════════════════════════════════════════════════════════

# Applied when no governance rules are found in the PDF.
# Ensures every imported project always has at least the two core rules.
DEFAULT_RULES = [
    {
        "rule_type":   "MOM_SLA",
        "description": "Minutes of Meeting must be sent after every meeting",
        "sla_hours":   6,
        "frequency":   "per_meeting",
    },
    {
        "rule_type":   "WBR_SLA",
        "description": "Weekly Business Review report to be shared with client",
        "sla_hours":   24,
        "frequency":   "weekly",
    },
]

# Only these rule types are recognised — keeps DB clean
RULE_TYPES = ["MOM_SLA", "WBR_SLA"]


# ══════════════════════════════════════════════════════════════════════════════
# PDF TEXT EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

def extract_text_from_pdf(pdf_path: str) -> str:
    """
    Read all pages of a PDF and return the full text as a single string.
    Pages are joined with newlines to preserve line-based field parsing.
    """
    reader = PdfReader(pdf_path)
    text = ""
    for page in reader.pages:
        text += page.extract_text() + "\n"
    return text


# ══════════════════════════════════════════════════════════════════════════════
# FIELD PARSERS
# ══════════════════════════════════════════════════════════════════════════════

def find_field(text: str, *labels) -> str:
    """
    Search for a labelled field in PDF text and return its value.

    Tries each label variant in order and returns the first match.
    Handles both "Label: value" and "Label — value" formats.

    Truncates at the next label-like line (Capital Word:) so values
    from adjacent fields don't bleed into each other.

    Example:
      find_field(text, "Project Name", "Project") →  "Retail ERP"
    """
    for label in labels:
        pattern = re.escape(label) + r"\s*[:\-]?\s*(.+)"
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            value = m.group(1).strip()
            # Stop at the next "Label:" line to avoid capturing the next field
            value = re.split(r"\n[A-Z][a-zA-Z ]+:", value)[0].strip()
            return value
    return ""


def parse_date(val: str):
    """
    Try parsing a date string against common formats used in project briefs.
    Returns a datetime object or None if no format matches.

    Supported formats: YYYY-MM-DD, DD-MM-YYYY, DD/MM/YYYY,
                       YYYY/MM/DD, Mon DD YYYY, Month DD YYYY
    """
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y",
                "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(val.strip(), fmt)
        except ValueError:
            continue
    return None


def parse_pct(val: str) -> float:
    """
    Extract the first number from a string and return it as a float.
    Used for delivery percentage fields e.g. "45%" → 45.0, "45 percent" → 45.0.
    Returns 0.0 if no number is found.
    """
    m = re.search(r"[\d.]+", val)
    return float(m.group()) if m else 0.0


# ══════════════════════════════════════════════════════════════════════════════
# GOVERNANCE RULES PARSER
# ══════════════════════════════════════════════════════════════════════════════

def parse_rules(text: str) -> list:
    """
    Extract governance rules from PDF text using regex.

    Looks for lines matching the pattern:
      MOM_SLA <description> per_meeting|weekly|biweekly ... SLA <hours>h

    Returns a list of rule dicts. Falls back to DEFAULT_RULES if none found,
    ensuring every project always has at least the two core rules.
    """
    rules = []

    for rt in RULE_TYPES:
        # Match rule type followed by description and frequency keyword
        pattern = rf"{rt}\s+(.+?)(?:per_meeting|weekly|biweekly).*?(\d+)h?"
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            desc = m.group(1).strip().rstrip("|").strip()

            # Search the next 200 chars after the match for frequency and SLA hours
            context = text[m.start(): m.start() + 200]
            freq_m  = re.search(r"(per_meeting|weekly|biweekly)", context, re.IGNORECASE)
            sla_m   = re.search(r"SLA\s*(\d+)", context, re.IGNORECASE)

            rules.append({
                "rule_type":   rt,
                "description": desc,
                "sla_hours":   int(sla_m.group(1)) if sla_m else 0,
                "frequency":   freq_m.group(1).lower() if freq_m else "weekly",
            })

    # Fall back to defaults if PDF contained no recognisable rule definitions
    return rules if rules else DEFAULT_RULES


# ══════════════════════════════════════════════════════════════════════════════
# MAIN EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

def extract_from_pdf(pdf_path: str) -> dict:
    """
    Read a PDF brief and extract all project fields into a dictionary.

    Field extraction uses find_field() with multiple label variants to handle
    different PDF template styles (e.g. "Client" vs "Client Name").

    Project ID fallback order:
      1. Found in PDF under "Project ID" or "Project Code"
      2. Parsed from the PDF filename  (e.g. PRJ-1001_brief.pdf → PRJ-1001)
      3. Generated from current time   (e.g. PRJ-1423)

    Returns a dict ready to be passed to import_pdf() or the Streamlit importer.
    """
    if not os.path.exists(pdf_path):
        print(f"❌ File not found: {pdf_path}")
        sys.exit(1)

    print(f"📄 Reading PDF: {pdf_path}")
    text = extract_text_from_pdf(pdf_path)

    # Extract each field trying multiple label variants per field
    project_id    = find_field(text, "Project ID",       "Project Code")
    name          = find_field(text, "Project Name",     "Project")
    client_name   = find_field(text, "Client",           "Client Name")
    description   = find_field(text, "Description")
    delivery_lead = find_field(text, "Delivery Lead",    "Lead")
    engagement    = find_field(text, "Engagement",       "Contract")
    start_raw     = find_field(text, "Start Date",       "Start")
    golive_raw    = find_field(text, "Go-Live Date",     "Go-Live", "GoLive")
    pct_raw       = find_field(text, "Delivery %",       "Progress", "Delivery Pct")
    gmail_query   = find_field(text, "Gmail Query",      "Gmail")
    cal_keyword   = find_field(text, "Calendar Keyword", "Calendar")

    # ── Project ID fallback ───────────────────────────────────────────────────
    if not project_id:
        base = os.path.splitext(os.path.basename(pdf_path))[0]
        m = re.search(r"PRJ-\d+", base, re.IGNORECASE)
        # Use ID from filename if present, otherwise generate one from current time
        project_id = m.group().upper() if m else f"PRJ-{datetime.now().strftime('%H%M')}"

    return {
        "project_id":       project_id,
        "name":             name          or "Unnamed Project",
        "client_name":      client_name   or "",
        "description":      description   or "",
        "delivery_lead":    delivery_lead or "",
        "engagement":       engagement    or "",
        # Dates are stored as "YYYY-MM-DD" strings; import_pdf() converts to datetime
        "start_date":       datetime.strftime(parse_date(start_raw),   "%Y-%m-%d") if parse_date(start_raw)   else None,
        "go_live_date":     datetime.strftime(parse_date(golive_raw),  "%Y-%m-%d") if parse_date(golive_raw)  else None,
        "delivery_pct":     parse_pct(pct_raw) if pct_raw else 0.0,
        # Default Gmail query targets subject line if none found in PDF
        "gmail_query":      gmail_query   or f"subject:{project_id}",
        "calendar_keyword": cal_keyword   or "",
        "governance_rules": parse_rules(text),
    }


# ══════════════════════════════════════════════════════════════════════════════
# SAVE TO DATABASE (CLI entry point)
# ══════════════════════════════════════════════════════════════════════════════

def import_pdf(pdf_path: str):
    """
    Extract project details from a PDF and save to the database (CLI flow).

    Shows extracted details and governance rules for review, then asks
    for confirmation before writing to the DB.

    If the project ID already exists, updates its fields instead of
    creating a duplicate. Governance rules are always replaced on re-import
    to stay in sync with the PDF.
    """
    data = extract_from_pdf(pdf_path)

    # ── Preview extracted data ────────────────────────────────────────────────
    print("\n── Extracted Project Details ────────────────────────")
    for k, v in data.items():
        if k != "governance_rules":   # rules printed separately below
            print(f"  {k:<20} : {v}")

    print(f"\n── Governance Rules ({len(data.get('governance_rules', []))})")
    for r in data.get("governance_rules", []):
        print(f"  [{r['rule_type']:<10}] {r['description']} "
              f"| {r['frequency']} | SLA {r['sla_hours']}h")

    # ── Confirm before saving ─────────────────────────────────────────────────
    confirm = input("\n✅ Save this to database? (yes/no): ").strip().lower()
    if confirm not in ("yes", "y"):
        print("Cancelled.")
        return

    db = Session()

    # ── Upsert: update if exists, create if new ───────────────────────────────
    existing = db.query(Project).filter_by(project_id=data.get("project_id")).first()

    if existing:
        print(f"⚠️  Project {data['project_id']} already exists. Updating details...")
        # Only update fields that were actually found in the PDF
        for field in ["name", "client_name", "description", "delivery_lead",
                      "engagement", "delivery_pct", "gmail_query", "calendar_keyword"]:
            if data.get(field) is not None:
                setattr(existing, field, data[field])
        # Dates need explicit conversion from string back to datetime
        if data.get("start_date"):
            existing.start_date = datetime.strptime(data["start_date"], "%Y-%m-%d")
        if data.get("go_live_date"):
            existing.go_live_date = datetime.strptime(data["go_live_date"], "%Y-%m-%d")
        project = existing

    else:
        project = Project(
            project_id       = data["project_id"],
            name             = data["name"],
            client_name      = data["client_name"],
            description      = data["description"],
            delivery_lead    = data["delivery_lead"],
            engagement       = data["engagement"],
            delivery_pct     = data["delivery_pct"],
            gmail_query      = data["gmail_query"],
            calendar_keyword = data["calendar_keyword"],
            start_date       = datetime.strptime(data["start_date"],   "%Y-%m-%d") if data.get("start_date")   else None,
            go_live_date     = datetime.strptime(data["go_live_date"], "%Y-%m-%d") if data.get("go_live_date") else None,
        )
        db.add(project)
        db.flush()   # flush to get project.id before inserting governance rules

    # ── Replace governance rules ──────────────────────────────────────────────
    # Delete first to avoid duplicates when re-importing the same PDF.
    db.query(GovernanceRule).filter_by(project_id=project.id).delete()
    for r in data.get("governance_rules", []):
        db.add(GovernanceRule(
            project_id  = project.id,
            rule_type   = r.get("rule_type",   "CUSTOM"),
            description = r.get("description", ""),
            sla_hours   = int(r.get("sla_hours", 0)),
            frequency   = r.get("frequency",   "weekly"),
        ))

    db.commit()

    # Read values before closing session to avoid DetachedInstanceError
    saved_id   = project.project_id
    saved_name = project.name
    db.close()

    print(f"\n✅ Project {saved_id} — {saved_name} saved successfully.")
    print(f"   Run: python main.py sync {saved_id}")