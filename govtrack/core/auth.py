"""
Authentication and project-visibility helpers for GovTrack.

Passwords are stored as salted PBKDF2 hashes. Google sign-in users should be
created in the same app_users table with auth_provider="google" and matched by
email after OAuth verifies the identity.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

from govtrack.core.models import AppUser, Project, ProjectAccess


ROLE_ADMIN = "admin"
ROLE_GLOBAL_VIEWER = "global_viewer"
ROLE_PROJECT_MANAGER = "project_manager"
GLOBAL_ROLES = {ROLE_ADMIN, ROLE_GLOBAL_VIEWER}


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def hash_password(password: str) -> str:
    """Return a salted password hash safe for DB storage."""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        (password or "").encode("utf-8"),
        salt,
        200_000,
    )
    return "pbkdf2_sha256$200000${}${}".format(
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored_hash: str) -> bool:
    """Check a plaintext password against a stored salted hash."""
    if not password or not stored_hash:
        return False
    try:
        algorithm, iterations, salt_b64, digest_b64 = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            int(iterations),
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def authenticate_password(session, email: str, password: str) -> AppUser | None:
    """Authenticate an active password user by email/password."""
    user = session.query(AppUser).filter_by(email=normalize_email(email), is_active=True).first()
    if user and user.auth_provider == "password" and verify_password(password, user.password_hash):
        return user
    return None


def user_can_see_all(user: AppUser | None) -> bool:
    return bool(user and user.role in GLOBAL_ROLES)


def visible_projects_query(session, user: AppUser | None):
    """Return a SQLAlchemy query containing only projects visible to this user."""
    query = session.query(Project)
    if not user:
        return query.filter(False)
    if user_can_see_all(user):
        return query

    mapped_ids = (
        session.query(ProjectAccess.project_id)
        .filter(ProjectAccess.user_id == user.id)
        .subquery()
    )
    return query.filter(
        (Project.pm_email == user.email) | (Project.id.in_(mapped_ids))
    )


def ensure_user(session, email: str, name: str, role: str, password: str | None = None) -> AppUser:
    """Create or update a user. Password is changed only when provided."""
    clean_email = normalize_email(email)
    user = session.query(AppUser).filter_by(email=clean_email).first()
    if not user:
        user = AppUser(email=clean_email)
        session.add(user)

    user.name = (name or clean_email).strip()
    user.role = role or ROLE_PROJECT_MANAGER
    user.auth_provider = "password" if password else (user.auth_provider or "password")
    user.is_active = True
    if password:
        user.password_hash = hash_password(password)
    return user


def replace_project_access(session, user: AppUser, projects: list[Project], access: str = "manager") -> None:
    """Replace explicit project mappings for one user."""
    session.query(ProjectAccess).filter_by(user_id=user.id).delete()
    for project in projects:
        session.add(ProjectAccess(user_id=user.id, project_id=project.id, access=access))
