# AMSF Deployment SOP

Use this SOP every time you move changes from your development machine to the Linux laptop server.

## Golden Rules

- Never copy your local `amsf.db` to production unless you intentionally want to replace all live data.
- Treat production `amsf.db` as the source of truth.
- Code changes go through Git. Production data changes go through migration scripts.
- Stop the app before schema migrations on SQLite.
- Always make a production DB backup before running a migration.
- Test risky DB changes on a copy of production, not on the live DB first.

## Release Types

### Code-Only Release

Use this when you changed Python, HTML, CSS, JS, docs, or config examples, but did not change:

- SQLAlchemy models in `database.py`
- table names
- column names/types/defaults
- indexes/constraints
- seed/import assumptions that affect existing rows
- data transformation logic needed by existing production rows

Deployment:

```bash
cd /path/to/amsf-app
git status --short
git pull --ff-only
uv sync --frozen
sudo systemctl restart amsf
sudo systemctl status amsf --no-pager
sudo journalctl -u amsf -n 100 --no-pager
```

If you run with `nohup ./run.sh` instead of systemd:

```bash
cd /path/to/amsf-app
git pull --ff-only
uv sync --frozen
pkill -f "uvicorn main:app"
nohup ./run.sh > amsf.log 2>&1 &
tail -n 100 amsf.log
```

### DB-Change Release

Use this when any release needs schema or data changes.

Before pushing the release:

1. Update the SQLAlchemy models in `database.py`.
2. Add additive/idempotent migration logic to `init_db()` or a dedicated migration script.
3. Update `migrate_db.py` expected schema checks when new tables/columns are required.
4. Test the migration against a copy of production data.
5. Commit the app change and migration together.

Production deployment:

```bash
cd /path/to/amsf-app
sudo systemctl stop amsf
git status --short
git pull --ff-only
uv sync --frozen
uv run python migrate_db.py
sudo systemctl start amsf
sudo systemctl status amsf --no-pager
sudo journalctl -u amsf -n 100 --no-pager
```

`migrate_db.py` performs an integrity check, creates a timestamped backup, applies schema updates, and verifies expected schema.

## How To Handle Local DB Changes

Most local DB changes should not be deployed as a copied database file.

Good:

- You add a new column locally.
- You write migration code that adds that column if missing.
- Production runs the same migration on its own live DB.

Bad:

- You add/test rows locally.
- You copy local `amsf.db` to production.
- Production live member payments, approvals, logs, and password changes get overwritten.

If you need to intentionally change production data, create a small, reviewed script that updates only the intended rows. Run it once after backup, then keep or archive the script with the release notes.

## Testing DB Changes Safely

On the production laptop, before touching live DB:

```bash
cd /path/to/amsf-app
mkdir -p db-test
sqlite3 amsf.db ".backup 'db-test/amsf-test.db'"
AMSF_DATABASE_URL=sqlite:///./db-test/amsf-test.db uv run python migrate_db.py
```

Then inspect the test DB through the app or with SQLite:

```bash
AMSF_DATABASE_URL=sqlite:///./db-test/amsf-test.db uv run uvicorn main:app --host 127.0.0.1 --port 8001
```

Only run the migration on live `amsf.db` after the copied DB works.

## Backup And Restore

Backups are created automatically by `migrate_db.py` in `AMSF_BACKUP_DIR`, or `./backups` by default.

If you want off-machine backups in Google Drive, use [backup_db_to_google_drive.py](backup_db_to_google_drive.py).

Recommended Drive backup flow:

1. Create a dedicated Drive folder for AMSF database backups.
2. Share that folder with the service account email from your Google credentials JSON.
3. Set `AMSF_GOOGLE_DRIVE_FOLDER_ID` and `AMSF_GOOGLE_SERVICE_ACCOUNT_FILE` on the server.
4. Schedule `uv run python backup_db_to_google_drive.py` every hour or after your chosen change window.
5. Keep only the latest few backups in Drive with `AMSF_GOOGLE_DRIVE_KEEP_LAST`.

### Linux systemd backup service

Use this if the app itself is already running as a systemd service and you want the backup job managed the same way.

Recommended files from this repo:

- [systemd/amsf-backup.service](systemd/amsf-backup.service)
- [systemd/amsf-backup.timer](systemd/amsf-backup.timer)

Step-by-step on the Linux server:

1. Clone or pull the repo to the server.
2. Put the Drive folder ID and OAuth paths into the repo's `.env` file, for example `/opt/apps/amsf-app/.env`.
3. Put your Google OAuth client JSON in the repo's `creds/` folder, for example `/opt/apps/amsf-app/creds/google-oauth-client.json`.
4. Copy [systemd/amsf-backup.service](systemd/amsf-backup.service) and [systemd/amsf-backup.timer](systemd/amsf-backup.timer) to `/etc/systemd/system/`.
5. If your repo path or Linux user is different, edit the service file; the committed template assumes `/opt/apps/amsf-app` and user `guna999`.
6. Reload systemd with `sudo systemctl daemon-reload`.
7. Enable and start the timer with `sudo systemctl enable --now amsf-backup.timer`.
8. Verify with `sudo systemctl status amsf-backup.timer` and `sudo systemctl list-timers --all`.
9. Run one immediate backup with `sudo systemctl start amsf-backup.service`.
10. Check the logs with `sudo journalctl -u amsf-backup.service -n 100 --no-pager`.
11. Confirm that a new backup file appears in the Drive folder.

Suggested `.env` entries in the repo root:

```bash
AMSF_GOOGLE_DRIVE_FOLDER_ID=your-drive-folder-id
AMSF_GOOGLE_OAUTH_CLIENT_FILE=/opt/apps/amsf-app/creds/google-oauth-client.json
AMSF_GOOGLE_OAUTH_TOKEN_FILE=/opt/apps/amsf-app/creds/google-oauth-token.json
AMSF_GOOGLE_DRIVE_KEEP_LAST=7
```

One-time local authorization step:

```bash
cd /opt/apps/amsf-app
uv run python backup_db_to_google_drive.py --authorize
```

Run that once on a machine where a browser can open, then copy the generated token file to the server if needed. After that, systemd can run the backup job without any manual login.

If you want backup timing to be different, change `OnCalendar=hourly` in the timer file. Examples:

- `OnCalendar=*:0/30` for every 30 minutes
- `OnCalendar=*-*-* 02:00:00` for 2 AM daily
- `OnCalendar=daily` for once per day

Manual backup:

```bash
cd /path/to/amsf-app
mkdir -p backups
sqlite3 amsf.db ".backup 'backups/amsf-manual-$(date +%Y%m%d-%H%M%S).db'"
```

Restore after a failed DB-change release:

```bash
cd /path/to/amsf-app
sudo systemctl stop amsf
cp backups/amsf-YYYYMMDD-HHMMSS.db amsf.db
git checkout <previous-good-commit>
uv sync --frozen
sudo systemctl start amsf
sudo systemctl status amsf --no-pager
```

Replace `amsf-YYYYMMDD-HHMMSS.db` and `<previous-good-commit>` with the real backup file and commit hash.

## Migration Rules For SQLite

Prefer safe additive migrations:

- `CREATE TABLE IF NOT EXISTS ...`
- `ALTER TABLE ... ADD COLUMN ...`
- populate nullable columns with defaults
- create new tables for logs/history

Avoid risky migrations unless you have a tested rollback:

- dropping columns
- renaming columns
- changing column types
- rebuilding tables
- deleting or rewriting financial records

For risky changes, make a one-off migration plan:

1. Backup.
2. Test on a copy of production.
3. Stop app.
4. Run migration.
5. Verify counts and important screens.
6. Start app.
7. Keep rollback instructions ready.

## Pre-Deploy Checklist

- `git status --short` is clean except intentional files.
- App starts locally.
- Syntax/tests pass.
- You know whether this is code-only or DB-change.
- `.env` on production points to the production DB.
- You are not committing `.env`, `amsf.db`, backups, or logs.

## Post-Deploy Smoke Test

After every deploy:

- Open `/login`.
- Login as custodian.
- Open `/admin`.
- Check contribution approval queue.
- Check monthly minimum pending list.
- Open a normal member dashboard.
- Confirm `journalctl` or `amsf.log` has no startup errors.

## Monthly Reminder Job

Use a systemd timer with `Persistent=true` for reminders. That way, if the laptop was powered off at the scheduled time, Linux runs the missed job after boot.

Cron is simpler, but normal cron does not catch up missed runs after a power outage.
