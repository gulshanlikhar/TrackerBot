# GovTrack Project Structure

GovTrack is organized by responsibility. The project root is folder-first:
source code, config, data, logs, docs, and scripts each live in their own directory.

## Folders

- `govtrack/core/` - database models, Google auth, shared paths.
- `govtrack/ai/` - email classification, summaries, and alert text helpers.
- `govtrack/integrations/` - Gmail, Calendar, and PDF ingestion.
- `govtrack/services/` - background watcher, notifications, and governance alerts.
- `govtrack/ui/` - Streamlit dashboard and PM confirmation page.
- `govtrack/cli/` - terminal command interface.
- `scripts/` - one-off maintenance and diagnostic scripts.
- `config/` - local config and requirements. Private files are ignored by Git.
- `data/` - local database and OAuth token. Ignored by Git.
- `logs/` - runtime logs. Ignored by Git.
- `docs/` - project notes and sample documents.

## Commands

```powershell
python -m streamlit run govtrack/ui/streamlit_app.py
python -m govtrack.cli.main list
python -m govtrack.services.Email_watcher
python scripts/run_govtrack.py
```
