"""
models.py — Database schema for GovTrack.

Defines all SQLAlchemy ORM models (tables) and two utility functions:
  - init_db()   : creates all tables from scratch
  - migrate_db(): safely adds new columns to an existing DB on startup
"""

from sqlalchemy import (create_engine, Column, Integer, String,
                        Float, Boolean, DateTime, Text, ForeignKey)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime
import os
from dotenv import load_dotenv
from govtrack.core.paths import DATABASE_PATH, ENV_PATH

# ── Environment & Database Setup ──────────────────────────────────────────────

load_dotenv(ENV_PATH)

# Use DATABASE_URL from .env if provided; otherwise default to local SQLite file.
# Also fix the case where .env contains the bare relative path "sqlite:///govtrack.db"
# — replace it with the absolute path so it works from any working directory.
database_url = os.getenv("DATABASE_URL", f"sqlite:///{DATABASE_PATH.as_posix()}")
if database_url.strip().lower() == "sqlite:///govtrack.db":
    database_url = f"sqlite:///{DATABASE_PATH.as_posix()}"

# check_same_thread=False is required for SQLite when used with Streamlit/threading
engine  = create_engine(database_url, connect_args={"check_same_thread": False})
Session = sessionmaker(bind=engine)   # call Session() to open a DB session
Base    = declarative_base()          # all models inherit from this


# ══════════════════════════════════════════════════════════════════════════════
# PROJECT
# ══════════════════════════════════════════════════════════════════════════════

class Project(Base):
    """
    Core entity. Represents one client engagement being tracked.
    Every Email, Meeting, Alert, GovernanceRule, and Member belongs to a Project.
    """
    __tablename__ = "projects"

    id               = Column(Integer, primary_key=True)
    project_id       = Column(String, unique=True, nullable=False)  # e.g. PRJ-1001
    name             = Column(String)                                # human-readable project name
    client_name      = Column(String)                                # client company name
    description      = Column(Text)                                  # brief project description
    delivery_lead    = Column(String)                                # name of the delivery lead
    engagement       = Column(String)                                # e.g. "Fixed-Price 18mo"
    start_date       = Column(DateTime)
    go_live_date     = Column(DateTime)
    delivery_pct     = Column(Float,   default=0.0)                  # 0–100 delivery progress
    health           = Column(String,  default="green")              # green / orange / red
    gmail_query      = Column(String)                                # Gmail search query for fetching emails
    calendar_keyword = Column(String)                                # keyword used to find calendar events
    pm_email         = Column(String)                                # PM's email for notifications
    pm_confirmed     = Column(Boolean, default=False)                # True once PM confirms via magic link
    pm_confirm_token = Column(String,  nullable=True)                # one-time token sent in confirmation email
    created_at       = Column(DateTime, default=datetime.utcnow)

    # Relationships — cascade delete ensures child rows are removed with the project
    emails    = relationship("Email",          back_populates="project", cascade="all, delete")
    meetings  = relationship("Meeting",        back_populates="project", cascade="all, delete")
    alerts    = relationship("Alert",          back_populates="project", cascade="all, delete")
    gov_rules = relationship("GovernanceRule", back_populates="project", cascade="all, delete")
    members   = relationship("Member",         back_populates="project", cascade="all, delete")


# ══════════════════════════════════════════════════════════════════════════════
# APP USER / PROJECT ACCESS
# ══════════════════════════════════════════════════════════════════════════════

class AppUser(Base):
    """
    A GovTrack application user.

    role controls visibility:
      admin          : can see and manage all projects/users
      global_viewer  : can see all projects
      project_manager: can see only mapped projects and projects where pm_email matches
    """
    __tablename__ = "app_users"

    id            = Column(Integer, primary_key=True)
    email         = Column(String, unique=True, nullable=False)
    name          = Column(String)
    role          = Column(String, default="project_manager")
    auth_provider = Column(String, default="password")  # password | google
    password_hash = Column(String, nullable=True)
    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime, default=datetime.utcnow)

    project_access = relationship("ProjectAccess", back_populates="user", cascade="all, delete")


class ProjectAccess(Base):
    """Explicit mapping between one user and one visible project."""
    __tablename__ = "project_access"

    id         = Column(Integer, primary_key=True)
    user_id    = Column(Integer, ForeignKey("app_users.id"))
    project_id = Column(Integer, ForeignKey("projects.id"))
    access     = Column(String, default="viewer")  # viewer | manager
    created_at = Column(DateTime, default=datetime.utcnow)

    user    = relationship("AppUser", back_populates="project_access")
    project = relationship("Project")


# ══════════════════════════════════════════════════════════════════════════════
# MEMBER
# ══════════════════════════════════════════════════════════════════════════════

class Member(Base):
    """
    A team member associated with a project.
    Can be added manually or auto-extracted from email senders/CC recipients.
    """
    __tablename__ = "members"

    id         = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"))
    name       = Column(String)
    email      = Column(String)
    role       = Column(String, default="Team Member")  # e.g. Developer, QA, PM, Client
    source     = Column(String, default="manual")       # "manual" (added by user) or "email" (auto-extracted)
    added_at   = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", back_populates="members")


# ══════════════════════════════════════════════════════════════════════════════
# GOVERNANCE RULE
# ══════════════════════════════════════════════════════════════════════════════

class GovernanceRule(Base):
    """
    A compliance rule attached to a project.
    Supported rule types: MOM_SLA (minutes of meeting) and WBR_SLA (weekly review).
    """
    __tablename__ = "governance_rules"

    id          = Column(Integer, primary_key=True)
    project_id  = Column(Integer, ForeignKey("projects.id"))
    rule_type   = Column(String)               # MOM_SLA | WBR_SLA
    description = Column(String)               # human-readable rule description
    sla_hours   = Column(Integer, default=0)   # hours allowed before breach (e.g. 48)
    frequency   = Column(String)               # per_meeting | weekly | biweekly
    status      = Column(String, default="ok") # ok | breached (updated by governance check)

    project = relationship("Project", back_populates="gov_rules")


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL
# ══════════════════════════════════════════════════════════════════════════════

class Email(Base):
    """
    A Gmail message linked to a project.
    Classified and summarised by email_rules.py at the time of fetch.
    """
    __tablename__ = "emails"

    id              = Column(Integer, primary_key=True)
    project_id      = Column(Integer, ForeignKey("projects.id"))
    gmail_msg_id    = Column(String, unique=True)         # Gmail message ID — prevents duplicates
    gmail_thread_id = Column(String, nullable=True)       # used for thread-based project matching
    subject         = Column(String)
    sender          = Column(String)                      # "Display Name <email@domain.com>"
    received_at     = Column(DateTime)
    category        = Column(String)                      # MoM | WBR
    risk_signal     = Column(Boolean, default=False)      # True if delay/escalation keywords detected
    snippet         = Column(Text)                        # short Gmail preview text
    summary         = Column(Text)                        # auto-generated summary with risk flags
    match_signal    = Column(String, nullable=True)       # how this email was matched: project_id / client_name / thread / ai

    project = relationship("Project", back_populates="emails")


# ══════════════════════════════════════════════════════════════════════════════
# MEETING
# ══════════════════════════════════════════════════════════════════════════════

class Meeting(Base):
    """
    A Google Calendar event linked to a project.
    Tracks whether MoM (Minutes of Meeting) was sent within the SLA window.
    """
    __tablename__ = "meetings"

    id             = Column(Integer, primary_key=True)
    project_id     = Column(Integer, ForeignKey("projects.id"))
    calendar_id    = Column(String, unique=True)           # Google Calendar event ID — prevents duplicates
    title          = Column(String)
    meeting_date   = Column(DateTime)
    attendees      = Column(Integer, default=0)
    mom_sent       = Column(Boolean, default=False)        # True once a MoM email is detected for this meeting
    mom_deadline   = Column(DateTime, nullable=True)       # meeting_date + MOM_SLA hours
    mom_alert_sent = Column(Boolean, default=False)        # True once overdue MoM alert email has been fired
    summary        = Column(Text)                          # auto-generated meeting summary

    project = relationship("Project", back_populates="meetings")


# ══════════════════════════════════════════════════════════════════════════════
# ALERT
# ══════════════════════════════════════════════════════════════════════════════

class Alert(Base):
    """
    A governance alert raised for a project.
    Generated by alerts.py after analysing emails, meetings, and rules.
    """
    __tablename__ = "alerts"

    id          = Column(Integer, primary_key=True)
    project_id  = Column(Integer, ForeignKey("projects.id"))
    level       = Column(String)                       # red | orange | green
    title       = Column(String)                       # short alert heading
    description = Column(Text)                         # detailed explanation
    resolved    = Column(Boolean, default=False)       # True once dismissed by the user
    created_at  = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", back_populates="alerts")


# ══════════════════════════════════════════════════════════════════════════════
# UNMAPPED EMAIL
# ══════════════════════════════════════════════════════════════════════════════

class UnmappedEmail(Base):
    """
    Stores incoming emails that contain work-related signals but could not be
    matched to any existing project. A manager alert is fired for each one.
    The manager can then manually assign it to a project from the dashboard.
    """
    __tablename__ = "unmapped_emails"

    id              = Column(Integer, primary_key=True)
    gmail_msg_id    = Column(String, unique=True, nullable=False)
    gmail_thread_id = Column(String, nullable=True)
    subject         = Column(String)
    sender          = Column(String)
    snippet         = Column(Text)
    body_preview    = Column(Text)                     # first 500 chars of body for manager preview
    received_at     = Column(DateTime, default=datetime.utcnow)
    alert_sent      = Column(Boolean, default=False)   # True once manager alert email has been fired

    # Filled in by the manager when they assign the email to a project from the dashboard
    assigned_project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)
    assigned_at         = Column(DateTime, nullable=True)
    assigned_by         = Column(String, nullable=True)   # "manual" or the manager's email


# ══════════════════════════════════════════════════════════════════════════════
# DB UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def init_db():
    """Create all tables defined above. Run once on first setup."""
    Base.metadata.create_all(engine)
    print("✅ Database tables created.")


def generate_project_id(session) -> str:
    """
    Generate the next system-owned project ID in PRJ-XXXX order.

    Both manual project creation and email-based auto creation must call this
    function so project IDs stay sequential and are never supplied by email text.
    """
    existing = session.query(Project.project_id).all()
    max_num = 0
    for (pid,) in existing:
        if not pid:
            continue
        try:
            num = int(str(pid).upper().replace("PRJ-", ""))
            max_num = max(max_num, num)
        except ValueError:
            continue
    return f"PRJ-{max_num + 1:04d}"


def migrate_db():
    """
    Safely add any missing columns to an existing database.
    Called automatically on every app startup — skips columns that already exist,
    so it is safe to run repeatedly without risk of data loss.

    Add new columns here whenever the schema changes instead of recreating the DB.
    """
    migrations = [
        # (table_name,      column_name,         column_definition)
        ("projects",        "pm_confirm_token",  "VARCHAR"),
        ("meetings",        "mom_alert_sent",    "BOOLEAN DEFAULT 0"),
        ("emails",          "match_signal",      "VARCHAR"),
        ("emails",          "gmail_thread_id",   "VARCHAR"),
        ("unmapped_emails", "assigned_by",       "VARCHAR"),
        ("unmapped_emails", "gmail_thread_id",   "VARCHAR"),
    ]

    with engine.connect() as conn:
        for table, col, col_def in migrations:
            try:
                conn.execute(
                    __import__("sqlalchemy").text(
                        f"ALTER TABLE {table} ADD COLUMN {col} {col_def}"
                    )
                )
                conn.commit()
                print(f"✅ Migration: added column {table}.{col}")
            except Exception:
                pass  # column already exists — safe to ignore
