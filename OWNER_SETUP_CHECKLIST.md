# AMSF Owner Setup Checklist

This file lists the remaining things that should be configured by you after the app code is in place.

## 1. Required: SMTP for Password Reset

The forgot-password flow is implemented, but real email sending only works after SMTP is configured.

Set these environment variables before starting the app:

- `AMSF_PUBLIC_BASE_URL`
- `AMSF_TIMEZONE` (use `Asia/Kolkata` for Kolkata, India)
- `AMSF_SMTP_HOST`
- `AMSF_SMTP_PORT` (usually `587`)
- `AMSF_SMTP_USER`
- `AMSF_SMTP_PASSWORD`
- `AMSF_SMTP_FROM`

Example PowerShell session:

```powershell
$env:AMSF_PUBLIC_BASE_URL="https://your-real-domain.com"
$env:AMSF_SMTP_HOST="smtp.gmail.com"
$env:AMSF_SMTP_PORT="587"
$env:AMSF_SMTP_USER="your-email@example.com"
$env:AMSF_SMTP_PASSWORD="your-app-password"
$env:AMSF_SMTP_FROM="your-email@example.com"
uv run uvicorn main:app --host 0.0.0.0 --port 8000
```

Notes:

- If you use Gmail, use an App Password, not your normal account password.
- If SMTP is not configured, reset links are only printed in the server console.

## 2. Required: Replace the Placeholder UPI QR

The app currently uses a placeholder asset:

- [static/img/upi-qr-placeholder.svg](/E:/guna-server/amsf-app/static/img/upi-qr-placeholder.svg)

To use your real UPI code:

1. Generate/export your actual UPI QR image.
2. Replace that file with your real QR image, or keep your own filename and update the image path in:
   [templates/dashboard.html](/E:/guna-server/amsf-app/templates/dashboard.html)
3. Reload the app and verify the QR displays correctly on the dashboard.

## 3. Strongly Recommended: Set a Real JWT Secret

Right now the app has a fallback development secret in code. For real use, set:

- `AMSF_SECRET_KEY`

Example:

```powershell
$env:AMSF_SECRET_KEY="replace-this-with-a-long-random-secret"
```

Use a long random value and keep it private.

## 4. Recommended: Decide Your Database Path and Backup Plan

The app uses SQLite and supports:

- `AMSF_DATABASE_URL`

Default:

```text
sqlite:///./amsf.db
```

If you want the DB in another location:

```powershell
$env:AMSF_DATABASE_URL="sqlite:///./amsf.db"
```

Recommended actions:

- Keep the database on a stable local disk.
- Back up `amsf.db` regularly.
- Stop the app before taking manual file-level backups if possible.
- Before deploying schema changes on Linux, stop the service and run `uv run python migrate_db.py`.
- The migration command creates an integrity-checked timestamped backup in `./backups` by default.
- If you want offsite backups, configure `AMSF_GOOGLE_DRIVE_FOLDER_ID` and `AMSF_GOOGLE_SERVICE_ACCOUNT_FILE`, then schedule `uv run python backup_db_to_google_drive.py`.

## 5. Recommended: Confirm the Default Admin Member

During seed/import, the app currently marks `B. GUNA` as admin/custodian automatically.

Check this assumption in:

- [database.py](/E:/guna-server/amsf-app/database.py)

If another person should be the custodian, update the seeding rule or adjust the database record.

Important:

- This matters mainly when creating a fresh database from Excel.
- If your DB is already created, changing the code alone will not retroactively change existing member roles.

## 6. Recommended: Decide the Bootstrap Password Policy

Fresh databases seed members with:

- default password `amsf123`

You can override this for future fresh database creation with:

- `AMSF_DEFAULT_PASSWORD`

Example:

```powershell
$env:AMSF_DEFAULT_PASSWORD="your-temporary-bootstrap-password"
```

Important:

- This only affects newly seeded databases.
- Existing users keep whatever password is already stored.

## 7. Recommended: Production Startup Method

For regular use on your home server, choose how the app should start automatically.

Current helper:

- [run.sh](/E:/guna-server/amsf-app/run.sh)

Suggested production command:

```powershell
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2
```

Decide one of these:

- Run manually when needed
- Use Task Scheduler on Windows
- Use NSSM or another service wrapper to run it as a service

If the app is running on Linux with systemd, keep the app service and backup timer separate. The app service runs `main.py`; the backup timer runs `backup_db_to_google_drive.py`.

## 8. Recommended: Schedule Monthly Reminder Emails

The app now includes:

- [send_reminders.py](/E:/guna-server/amsf-app/send_reminders.py)

What it does:

- Sends daily reminder emails from the 5th to the 10th for members who are still short on the monthly minimum contribution
- Sends daily due reminders from the 11th to the 15th for members still short
- Includes current contribution due and current loan repayment due
- Skips duplicate sends for the same day and reminder type

Recommended scheduler setup:

- On Windows, create a Task Scheduler task that runs once daily:

```powershell
uv run python send_reminders.py
```

- Set the working directory to the repo root: `E:\guna-server\amsf-app`
- Pick a fixed morning time such as `09:00`

## 9. Recommended: Verify Password Reset End-to-End

After SMTP is configured:

1. Open `/forgot-password`
2. Submit a member name that already completed setup with a real email
3. Confirm the email arrives
4. Open the reset link
5. Set a new password
6. Confirm login works with the new password

## 10. Optional: Replace Placeholder Branding / Text

You may want to personalize:

- App title and navbar text in [templates/base.html](/E:/guna-server/amsf-app/templates/base.html)
- Dashboard copy in [templates/dashboard.html](/E:/guna-server/amsf-app/templates/dashboard.html)
- Colors/styles in [static/css/style.css](/E:/guna-server/amsf-app/static/css/style.css)

## 11. Optional: Add a Real Domain or LAN Hostname

This is not optional for normal internet users if you want password reset emails to work reliably.

Password reset links should point to the public URL your users can actually open from outside your home network.

Set:

- `AMSF_PUBLIC_BASE_URL`

Examples:

- `https://amsf.yourdomain.com`
- `https://fund.yourdomain.com`

It is only optional if:

- you do not plan to use password reset emails yet, or
- everyone accesses the app through one stable host and that host is always the same one seen by the app

For internet-facing usage, you should set a real public domain or public HTTPS host.

## Quick Summary

You definitely still need:

- SMTP setup
- Real UPI QR replacement
- Real `AMSF_SECRET_KEY`
- Real `AMSF_PUBLIC_BASE_URL`

You should also review:

- DB path and backup plan
- Admin/custodian assignment
- Bootstrap password policy
- Production startup/service setup
