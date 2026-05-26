"""
confirm_page.py — PM Project Confirmation Form
================================================
This module is called from streamlit_app.py when the URL contains
?confirm_token=<token>.

The PM lands here after clicking the link in the GovTrack email.
They can edit all auto-detected project details and submit confirmation.

Usage in streamlit_app.py:
    from confirm_page import render_confirm_page
    render_confirm_page(token)
"""

import streamlit as st
from datetime import datetime
from govtrack.core.models import Session, Project, Member, GovernanceRule

try:
    from govtrack.services.Notifier import send_project_confirmation
except ImportError:
    from govtrack.services.Notifier import send_project_confirmation


# ── CSS shared with main app ──────────────────────────────────────────────────

CONFIRM_CSS = """
<style>
.cf-wrap   { max-width:700px; margin:0 auto; padding:0 1rem 4rem; }
.cf-header { background:#1A1A18; border-radius:12px; padding:24px 28px; margin-bottom:1.5rem; }
.cf-step   { background:#FFFFFF; border:1px solid #E5E4DE; border-radius:12px;
             padding:20px 22px; margin-bottom:1rem; }
.cf-step-label { font-size:11px; font-weight:600; color:#5C3FD4;
                 text-transform:uppercase; letter-spacing:.07em; margin-bottom:12px; }
.cf-done   { background:#D6F0E0; border:1px solid #A3D9B8; border-radius:12px;
             padding:28px; text-align:center; margin-top:1rem; }
</style>
"""


def _db():
    return Session()


def render_confirm_page(token: str):
    """Render the full editable confirmation form for the given token."""

    st.markdown(CONFIRM_CSS, unsafe_allow_html=True)

    # ── Look up the project by token ─────────────────────────────────────────
    s = _db()
    project = s.query(Project).filter_by(pm_confirm_token=token).first()

    if not project:
        s.close()
        st.markdown("""
        <div style="max-width:500px;margin:4rem auto;text-align:center">
          <div style="font-size:48px;margin-bottom:1rem">🔗</div>
          <div style="font-size:20px;font-weight:600;color:#1A1A18;margin-bottom:.5rem">
            Link Not Found
          </div>
          <div style="font-size:14px;color:#6C6B63">
            This confirmation link is invalid or has already been used.<br>
            If you need to re-confirm, please contact your GovTrack administrator.
          </div>
        </div>""", unsafe_allow_html=True)
        return

    if project.pm_confirmed:
        s.close()
        st.markdown(f"""
        <div class="cf-done">
          <div style="font-size:36px;margin-bottom:.75rem">✅</div>
          <div style="font-size:18px;font-weight:600;color:#1A5C38;margin-bottom:.4rem">
            Already Confirmed
          </div>
          <div style="font-size:13px;color:#3A6A50">
            Project <b>{project.project_id} — {project.name}</b> has already been confirmed.<br>
            You can view it on the GovTrack dashboard.
          </div>
        </div>""", unsafe_allow_html=True)
        return

    # Load members
    members = s.query(Member).filter_by(project_id=project.id).all()
    client_members = [m for m in members if m.role == "Client"]
    team_members   = [m for m in members if m.role not in ("Client", "Project Manager")]
    s.close()

    # ── Header ────────────────────────────────────────────────────────────────
    st.markdown(f"""
    <div class="cf-header">
      <div style="font-size:20px;font-weight:700;color:#FFFFFF;margin-bottom:4px">🛡️ GovTrack — Project Confirmation</div>
      <div style="font-size:13px;color:#8A8980">
        Please review and correct all auto-detected details, then submit your confirmation.
      </div>
    </div>
    <div style="font-size:14px;color:#6C6B63;margin-bottom:1.5rem;padding:0 2px">
      Project detected: &nbsp;<b style="color:#1A1A18">{project.project_id} — {project.name}</b>
    </div>
    """, unsafe_allow_html=True)

    # ── Build the form ────────────────────────────────────────────────────────
    with st.form("pm_confirm_form", clear_on_submit=False):

        # ── Section 1: PM Identity ────────────────────────────────────────────
        st.markdown('<div class="cf-step">', unsafe_allow_html=True)
        st.markdown('<div class="cf-step-label">1 · Are you the Project Manager?</div>', unsafe_allow_html=True)
        st.markdown('<div style="font-size:12px;color:#6C6B63;margin-bottom:10px">We detected you as the sender. Please confirm your name and that you are the PM for this project.</div>', unsafe_allow_html=True)

        col1, col2 = st.columns(2)
        with col1:
            pm_name = st.text_input(
                "Your Full Name *",
                value=project.delivery_lead or "",
                placeholder="e.g. Priya Sharma",
                key="cf_pm_name",
            )
        with col2:
            pm_email_val = st.text_input(
                "Your Email *",
                value=project.pm_email or "",
                placeholder="pm@oneture.com",
                key="cf_pm_email",
            )
        st.markdown('</div>', unsafe_allow_html=True)

        # ── Section 2: Client ─────────────────────────────────────────────────
        st.markdown('<div class="cf-step">', unsafe_allow_html=True)
        st.markdown('<div class="cf-step-label">2 · Is this the correct client?</div>', unsafe_allow_html=True)
        st.markdown('<div style="font-size:12px;color:#6C6B63;margin-bottom:10px">We detected the client from the "To" field of your email. Please correct if needed.</div>', unsafe_allow_html=True)

        client_default_name  = client_members[0].name  if client_members else project.client_name or ""
        client_default_email = client_members[0].email if client_members else ""

        col3, col4 = st.columns(2)
        with col3:
            client_name_val = st.text_input(
                "Client Name *",
                value=client_default_name,
                placeholder="e.g. RetailMax India Pvt Ltd",
                key="cf_client_name",
            )
        with col4:
            client_email_val = st.text_input(
                "Client Contact Email",
                value=client_default_email,
                placeholder="contact@client.com",
                key="cf_client_email",
            )
        st.markdown('</div>', unsafe_allow_html=True)

        # ── Section 3: Team Members ───────────────────────────────────────────
        st.markdown('<div class="cf-step">', unsafe_allow_html=True)
        st.markdown('<div class="cf-step-label">3 · Are these the correct team members?</div>', unsafe_allow_html=True)
        st.markdown('<div style="font-size:12px;color:#6C6B63;margin-bottom:12px">We detected these members from the "Cc" field. Edit names/roles or add/remove below. Leave a row blank to skip it.</div>', unsafe_allow_html=True)

        # Show up to 8 editable member rows (pre-filled + 3 blank for new)
        MAX_ROWS = max(8, len(team_members) + 3)
        member_inputs = []

        # Header row
        hc1, hc2, hc3 = st.columns([3, 3, 2])
        hc1.markdown('<div style="font-size:11px;color:#8A8980;font-weight:500">Full Name</div>', unsafe_allow_html=True)
        hc2.markdown('<div style="font-size:11px;color:#8A8980;font-weight:500">Email</div>', unsafe_allow_html=True)
        hc3.markdown('<div style="font-size:11px;color:#8A8980;font-weight:500">Role</div>', unsafe_allow_html=True)

        ROLE_OPTIONS = ["Team Member", "Delivery Lead", "Developer", "Business Analyst",
                        "QA Engineer", "Scrum Master", "Architect", "Client"]

        for i in range(MAX_ROWS):
            existing = team_members[i] if i < len(team_members) else None
            mc1, mc2, mc3 = st.columns([3, 3, 2])
            with mc1:
                m_name = st.text_input(
                    f"Name {i+1}", label_visibility="collapsed",
                    value=existing.name if existing else "",
                    placeholder=f"Member {i+1} name",
                    key=f"cf_m_name_{i}",
                )
            with mc2:
                m_email = st.text_input(
                    f"Email {i+1}", label_visibility="collapsed",
                    value=existing.email if existing else "",
                    placeholder="email@example.com",
                    key=f"cf_m_email_{i}",
                )
            with mc3:
                default_role = existing.role if existing else "Team Member"
                role_idx = ROLE_OPTIONS.index(default_role) if default_role in ROLE_OPTIONS else 0
                m_role = st.selectbox(
                    f"Role {i+1}", ROLE_OPTIONS, index=role_idx,
                    label_visibility="collapsed",
                    key=f"cf_m_role_{i}",
                )
            member_inputs.append((m_name, m_email, m_role))

        st.markdown('</div>', unsafe_allow_html=True)

        # ── Section 4: Project Statement ──────────────────────────────────────
        st.markdown('<div class="cf-step">', unsafe_allow_html=True)
        st.markdown('<div class="cf-step-label">4 · Is this the correct project statement?</div>', unsafe_allow_html=True)
        st.markdown('<div style="font-size:12px;color:#6C6B63;margin-bottom:12px">All fields below were auto-extracted. Please edit anything that is incorrect.</div>', unsafe_allow_html=True)

        ps1, ps2 = st.columns(2)
        with ps1:
            st.text_input("Project ID", value=project.project_id, key="cf_proj_id", disabled=True)
        with ps2:
            proj_name_val = st.text_input("Project Name *", value=project.name or "", key="cf_proj_name")

        ps3, ps4 = st.columns(2)
        with ps3:
            engagement_val = st.text_input("Engagement / Contract Type",
                value=project.engagement or "", placeholder="e.g. Fixed-Price 18 months", key="cf_engagement")
        with ps4:
            delivery_pct_val = st.number_input("Delivery % Complete", min_value=0.0, max_value=100.0,
                value=float(project.delivery_pct or 0), step=1.0, key="cf_delivery_pct")

        ps5, ps6 = st.columns(2)
        with ps5:
            start_val = st.date_input("Start Date",
                value=project.start_date.date() if project.start_date else None,
                key="cf_start")
        with ps6:
            golive_val = st.date_input("Go-Live Date",
                value=project.go_live_date.date() if project.go_live_date else None,
                key="cf_golive")

        description_val = st.text_area("Project Description",
            value=project.description or "",
            placeholder="Brief description of the project scope and objectives",
            height=90,
            key="cf_description",
        )

        gmail_val = st.text_input("Gmail Search Query",
            value=project.gmail_query or f"subject:{project.project_id}",
            help="Used to auto-fetch project emails from Gmail. e.g. subject:PRJ-0001",
            key="cf_gmail",
        )
        cal_val = st.text_input("Calendar Keyword",
            value=project.calendar_keyword or "",
            help="Used to find calendar events for this project. e.g. RetailMax",
            key="cf_cal",
        )
        st.markdown('</div>', unsafe_allow_html=True)

        # ── Submit ────────────────────────────────────────────────────────────
        st.markdown('<div style="height:.5rem"></div>', unsafe_allow_html=True)
        submitted = st.form_submit_button(
            "🔏  Submit Confirmation",
            type="primary",
            use_container_width=True,
        )

    # ── Handle submission ─────────────────────────────────────────────────────
    if submitted:
        errors = []
        if not pm_name.strip():
            errors.append("Your full name is required (Section 1).")
        if not pm_email_val.strip() or "@" not in pm_email_val:
            errors.append("A valid PM email is required (Section 1).")
        if not client_name_val.strip():
            errors.append("Client name is required (Section 2).")
        if not proj_name_val.strip():
            errors.append("Project Name is required (Section 4).")

        if errors:
            for err in errors:
                st.error(err)
        else:
            try:
                s = _db()
                proj = s.query(Project).filter_by(pm_confirm_token=token).first()

                # ── Update project fields ─────────────────────────────────────
                proj.delivery_lead    = pm_name.strip()
                proj.pm_email         = pm_email_val.strip().lower()
                proj.client_name      = client_name_val.strip()
                proj.name             = proj_name_val.strip()
                proj.engagement       = engagement_val.strip()
                proj.delivery_pct     = float(delivery_pct_val)
                proj.description      = description_val.strip()
                proj.gmail_query      = gmail_val.strip()
                proj.calendar_keyword = cal_val.strip()
                proj.start_date       = datetime.combine(start_val, datetime.min.time()) if start_val else None
                proj.go_live_date     = datetime.combine(golive_val, datetime.min.time()) if golive_val else None
                proj.pm_confirmed     = True
                proj.pm_confirm_token = None   # invalidate token after use

                # ── Update PM member record ───────────────────────────────────
                pm_member = s.query(Member).filter_by(
                    project_id=proj.id, email=project.pm_email
                ).first()
                if pm_member:
                    pm_member.name  = pm_name.strip()
                    pm_member.email = pm_email_val.strip().lower()
                else:
                    s.add(Member(
                        project_id=proj.id,
                        name=pm_name.strip(),
                        email=pm_email_val.strip().lower(),
                        role="Project Manager",
                        source="email",
                    ))

                # ── Update / replace client member ────────────────────────────
                # Remove old client entries, add fresh one
                for cm in s.query(Member).filter_by(project_id=proj.id, role="Client").all():
                    s.delete(cm)
                if client_name_val.strip():
                    s.add(Member(
                        project_id=proj.id,
                        name=client_name_val.strip(),
                        email=client_email_val.strip().lower() if client_email_val.strip() else "",
                        role="Client",
                        source="email",
                    ))

                # ── Replace team members ──────────────────────────────────────
                for tm in s.query(Member).filter_by(project_id=proj.id).filter(
                    Member.role.notin_(["Client", "Project Manager"])
                ).all():
                    s.delete(tm)

                seen_emails = {pm_email_val.strip().lower(), client_email_val.strip().lower()}
                for m_name, m_email, m_role in member_inputs:
                    if not m_name.strip() and not m_email.strip():
                        continue   # blank row — skip
                    if m_email.strip().lower() in seen_emails:
                        continue   # duplicate — skip
                    if m_email.strip() and "@" not in m_email:
                        continue   # invalid email — skip
                    seen_emails.add(m_email.strip().lower())
                    s.add(Member(
                        project_id=proj.id,
                        name=m_name.strip() or m_email.split("@")[0].title(),
                        email=m_email.strip().lower(),
                        role=m_role,
                        source="confirmed",
                    ))

                s.commit()

                # ── Send the post-confirmation welcome email to PM + CC members ──
                all_members = s.query(Member).filter_by(project_id=proj.id).all()
                member_cc = [m.email for m in all_members
                             if m.email and m.email != proj.pm_email and m.role != "Client"]
                try:
                    send_project_confirmation(proj, proj.pm_email, member_cc)
                    print(f"✅ PM confirmation email sent to {proj.pm_email}")
                    print(f"✅ Member welcome emails sent to {len(member_cc)} member(s)")
                except Exception as mail_err:
                    print(f"⚠️ Welcome email failed: {mail_err}")

                s.close()

                # ── Success screen ────────────────────────────────────────────
                st.markdown(f"""
                <div class="cf-done">
                  <div style="font-size:40px;margin-bottom:.75rem">🎉</div>
                  <div style="font-size:20px;font-weight:700;color:#1A5C38;margin-bottom:.5rem">
                    Confirmation Submitted!
                  </div>
                  <div style="font-size:13px;color:#3A6A50;line-height:1.8">
                    Project <b>{proj.project_id} — {proj.name}</b> is now active in GovTrack.<br>
                    A welcome email has been sent to <b>{proj.pm_email}</b> and your team.<br><br>
                    You can close this tab and return to the GovTrack dashboard.
                  </div>
                </div>
                """, unsafe_allow_html=True)
                st.balloons()

            except Exception as ex:
                st.error(f"Something went wrong while saving: {ex}")
