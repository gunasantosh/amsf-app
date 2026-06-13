# AMSF Web App

Lightweight FastAPI + SQLite application for the Anonymous Mutual Support Fund.

## Features

- Excel bootstrap from `AMSF.xlsx` or `tracking.xlsx`
- First-login account setup with required email and password change
- Password reset flow with expiring reset tokens and SMTP support
- Member dashboard with due amount engine, payment alerts, Chart.js visualizations, loan request flow, and voting
- Custodian panel for contribution approval, loan sanctioning, and fund overview
- Append-only financial activity ledger with inline member payment history for custodian review
- Persisted operational email outbox showing queued, sent, skipped, and failed deliveries
- Decimal money handling for contribution, loan, and repayment calculations
- Investments placeholder page for future expansion

## Default Login

- Username: member `original_name` from the workbook
- Default password: `amsf123`

Set `AMSF_DEFAULT_PASSWORD` if you want a different bootstrap password for fresh databases.

## Environment Variables

- `AMSF_SECRET_KEY`: JWT signing secret
- `AMSF_DEFAULT_PASSWORD`: initial password used when seeding a new database
- `AMSF_DATABASE_URL`: SQLite database URL
- `AMSF_BACKUP_DIR`: optional directory for deployment database backups, default `./backups`
- `AMSF_PUBLIC_BASE_URL`: public app URL used in password reset emails
- `AMSF_TIMEZONE`: local calendar and display timezone, default `Asia/Kolkata`
- `AMSF_SMTP_HOST`: SMTP server hostname
- `AMSF_SMTP_PORT`: SMTP server port, default `587`
- `AMSF_SMTP_USER`: SMTP username
- `AMSF_SMTP_PASSWORD`: SMTP password
- `AMSF_SMTP_FROM`: optional sender email

The app automatically loads variables from `.env`.

If SMTP is not configured, password reset links are printed to the server console for manual testing.
Operational notifications are also recorded in the custodian outbox. This keeps failed or skipped deliveries visible instead of losing them silently.

## Run Locally

```bash
uv sync
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## Home Server Run

```bash
nohup ./run.sh > amsf.log 2>&1 &
```

`run.sh` uses `uv run uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2`, which is a reasonable fit for a 4-core / 3 GB RAM home server.

## Linux Production Deployment

Use [DEPLOYMENT_SOP.md](DEPLOYMENT_SOP.md) as the source of truth for production deploys.

Short version for code-only releases:

```bash
cd /path/to/amsf-app
git pull --ff-only
uv sync --frozen
sudo systemctl restart amsf
sudo systemctl status amsf --no-pager
```

For DB-change releases, stop the service, pull the release, run `uv run python migrate_db.py`, then start the service. `migrate_db.py` checks SQLite integrity, creates a timestamped SQLite backup, applies additive schema updates, and verifies the resulting schema.

## Monthly Reminder Job

The repo includes [send_reminders.py](/E:/guna-server/amsf-app/send_reminders.py) for scheduled email reminders.

Behavior:

- From the 5th to the 10th of each month: sends daily contribution reminders to members whose minimum monthly contribution is still short
- From the 11th to the 15th of each month: sends daily due reminders to members still short
- After the 15th: does nothing
- Prevents duplicate sends of the same reminder type on the same day

Run manually:

```bash
uv run python send_reminders.py
```

Recommended scheduling:

- Windows Task Scheduler: run once daily around 9:00 AM using `uv run python send_reminders.py`
- Linux cron/systemd timer: run once daily during the same window
