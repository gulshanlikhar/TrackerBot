"""
calendar_reader.py — Google Calendar meeting fetcher for GovTrack.

Fetches all calendar events matching a project's calendar_keyword,
calculates MoM deadlines from the MOM_SLA governance rule,
and saves new Meeting rows to the database.
"""

from datetime import datetime, timedelta, timezone
from govtrack.core.models import Session, Meeting, GovernanceRule
from govtrack.core.google_auth import calendar_service
from govtrack.ai.gemini import summarize_meeting


def fetch_meetings(project, days_back: int = 90, days_ahead: int = 30):
    """
    Fetch all Google Calendar events matching the project's calendar_keyword.

    Search window:
      - Past   : today minus days_back  (default 90 days)
      - Future : today plus days_ahead  (default 30 days)

    For each new event:
      1. Skip if already saved (dedup by calendar_id)
      2. Generate a one-line meeting summary
      3. Calculate MoM deadline = meeting_date + MOM_SLA hours (default 48h)
      4. Save Meeting row to DB

    Args:
      project    : Project ORM object (needs .project_id, .calendar_keyword, .id)
      days_back  : how many past days to search
      days_ahead : how many future days to search
    """
    service = calendar_service()
    db      = Session()
    new     = 0   # counter for newly saved meetings

    print(f"\n📅 Fetching meetings for {project.project_id} "
          f"— keyword: '{project.calendar_keyword}'")

    try:
        # ── Build search window ───────────────────────────────────────────────
        now      = datetime.now(timezone.utc)
        time_min = (now - timedelta(days=days_back)).isoformat()
        time_max = (now + timedelta(days=days_ahead)).isoformat()
        page_token = None

        # ── Paginated event fetch ─────────────────────────────────────────────
        # Google Calendar API returns max 250 events per page;
        # loop until nextPageToken is None (no more pages).
        while True:
            kwargs = dict(
                calendarId   = "primary",
                q            = project.calendar_keyword,  # free-text search across title/description
                timeMin      = time_min,
                timeMax      = time_max,
                singleEvents = True,    # expand recurring events into individual instances
                orderBy      = "startTime",
                maxResults   = 250,
            )
            if page_token:
                kwargs["pageToken"] = page_token

            result     = service.events().list(**kwargs).execute()
            events     = result.get("items", [])
            page_token = result.get("nextPageToken")

            for event in events:

                # ── Dedup: skip if this calendar event is already saved ───────
                if db.query(Meeting).filter_by(calendar_id=event["id"]).first():
                    continue

                # ── Parse start time ──────────────────────────────────────────
                # All-day events use "date" (no time); timed events use "dateTime".
                # Strip timezone info after parsing to keep datetimes naive (UTC).
                start    = event["start"].get("dateTime", event["start"].get("date"))
                start_dt = datetime.fromisoformat(
                    start.replace("Z", "+00:00")
                ).replace(tzinfo=None)

                attendees = len(event.get("attendees", []))
                title     = event.get("summary", "Meeting")  # "summary" is Google's field name for title

                # ── Generate meeting summary ──────────────────────────────────
                summary = summarize_meeting(
                    title     = title,
                    date      = start_dt.strftime("%b %d, %Y %H:%M"),
                    attendees = attendees,
                )

                # ── Calculate MoM deadline ────────────────────────────────────
                # Look up the MOM_SLA rule for this project to get the configured
                # SLA hours. Fall back to 48 hours if no rule is defined.
                rule = db.query(GovernanceRule).filter_by(
                    project_id = project.id,
                    rule_type  = "MOM_SLA",
                ).first()
                sla_hours    = rule.sla_hours if rule and rule.sla_hours else 48
                mom_deadline = start_dt + timedelta(hours=sla_hours)

                # ── Save meeting ──────────────────────────────────────────────
                db.add(Meeting(
                    project_id   = project.id,
                    calendar_id  = event["id"],   # unique Google Calendar event ID
                    title        = title,
                    meeting_date = start_dt,
                    attendees    = attendees,
                    mom_sent     = False,          # watcher sets this True when MoM email is detected
                    mom_deadline = mom_deadline,
                    summary      = summary,
                ))
                new += 1

                # Print status line — past vs upcoming makes it easy to scan
                status = "✅ Past" if start_dt < datetime.utcnow() else "🔜 Upcoming"
                print(f"  {status}  {start_dt.strftime('%b %d %H:%M')}  "
                      f"{title:<40} ({attendees} attendees)")
                print(f"         → {summary}")

            if not page_token:
                break   # all pages fetched

        db.commit()
        print(f"\n  ✅ {new} new meetings saved for {project.project_id}")

    except Exception as e:
        print(f"  ❌ Calendar error: {e}")
        db.rollback()   # roll back partial saves on error
    finally:
        db.close()      # always close session