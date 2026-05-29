# TrackerBot / GovTrack

TrackerBot is a project governance tracker that reads Gmail and Google Calendar
signals to help delivery teams monitor projects, emails, meetings, MoM status,
WBR status, risks, alerts, project members, clients, and PM confirmations.

The application is built with Streamlit, SQLite, Gmail API, Google Calendar API,
AWS Bedrock Converse, and a modular Python backend.

## Features

- Auto-detect project-related emails from Gmail.
- Create system-generated project IDs.
- Identify client name from CC, sender domain, and email body.
- Identify PM and team/client members from email sender, To, and CC.
- Attach emails to existing projects using project name, project ID, labels, or
  Gmail thread context.
- Detect and queue unmapped emails for manual attachment.
- Support one email/thread containing multiple project discussions.
- Send PM confirmation/claim emails.
- Track meetings from Google Calendar.
- Track MoM SLA and overdue MoM alerts.
- Track WBR governance signals.
- Show project dashboard with emails, meetings, members, alerts, and health.
- Import project details from PDF.
- Dark-themed Streamlit UI.

## Project Structure

```text
govtrack/
  ai/              Email classification, summaries, alert text helpers
  cli/             Command-line interface
  core/            Database models, Google auth, shared paths
  integrations/    Gmail, Calendar, and PDF integrations
  services/        Email watcher, notifier, governance alerts
  ui/              Streamlit dashboard and PM confirmation page

scripts/           Utility and diagnostic scripts
config/            Local config, requirements, Streamlit config
data/              Local database and OAuth token
docs/              Project documentation and sample files
logs/              Runtime logs
```

Private runtime files in `config/`, `data/`, and `logs/` are ignored by Git.

## Requirements

- Python 3.10+
- Gmail API credentials
- Google Calendar API access
- Python dependencies from `config/requirements.txt`

Install dependencies:

```powershell
python -m pip install -r config/requirements.txt
```

## Local Configuration

Create or update:

```text
config/.env
```

Example:

```env
DATABASE_URL=sqlite:///data/govtrack.db
CREDENTIALS_PATH=config/credentials.json
TOKEN_PATH=data/token.json
AWS_REGION=ap-south-1
BEDROCK_MODEL_ID=openai.gpt-oss-20b-1:0
GOVTRACK_EMAIL=client.update@oneture.com
```

Place your Google OAuth client file here:

```text
config/credentials.json
```

After first Google login, the app creates:

```text
data/token.json
```

## How To Run

Open PowerShell in the project folder:

```powershell
cd "C:\Users\user\Downloads\govtrack2 - Copy"
```

Run the Streamlit dashboard:

```powershell
python -m streamlit run govtrack/ui/streamlit_app.py
```

Run only the email watcher:

```powershell
python -m govtrack.services.Email_watcher
```

Run the app and watcher together:

```powershell
python scripts/run_govtrack.py
```

Run CLI commands:

```powershell
python -m govtrack.cli.main list
python -m govtrack.cli.main sync PRJ-2041
python -m govtrack.cli.main alerts PRJ-2041
python -m govtrack.cli.main show PRJ-2041
```

## Main Workflow

1. Start the Streamlit app.
2. GovTrack starts the Gmail watcher.
3. The watcher checks inbox emails.
4. Relevant project emails are matched to existing projects or used to create
   new projects.
5. PM receives a confirmation email.
6. PM confirms project details using the confirmation page.
7. Dashboard tracks emails, members, meetings, MoM, WBR, risks, and alerts.
8. Unclear emails go to the manual Attach Emails workflow.

## Important Git Safety

Do not commit these local/private files:

```text
config/.env
config/credentials.json
data/token.json
data/govtrack.db
logs/
```

They are intentionally ignored in `.gitignore`.

## Troubleshooting

If `streamlit` is not recognized:

```powershell
python -m streamlit run govtrack/ui/streamlit_app.py
```

If Gmail authentication fails:

```powershell
Remove-Item data/token.json
python -m streamlit run govtrack/ui/streamlit_app.py
```

If Bedrock returns an access error, confirm the IAM user/role has
`bedrock:InvokeModel` permission for the selected model and that model access is
enabled in the configured AWS region.
