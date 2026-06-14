from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from migrate_db import create_backup, database_path

GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


@dataclass(frozen=True)
class DriveBackupConfig:
    folder_id: str
    service_account_file: Path
    prefix: str
    keep_last: int


def load_config(default_prefix: str, keep_last_override: int | None = None) -> DriveBackupConfig:
    folder_id = os.getenv("AMSF_GOOGLE_DRIVE_FOLDER_ID", "").strip()
    if not folder_id:
        raise RuntimeError("AMSF_GOOGLE_DRIVE_FOLDER_ID is required.")

    service_account_file_value = (
        os.getenv("AMSF_GOOGLE_SERVICE_ACCOUNT_FILE")
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    ).strip()
    if not service_account_file_value:
        raise RuntimeError(
            "Set AMSF_GOOGLE_SERVICE_ACCOUNT_FILE or GOOGLE_APPLICATION_CREDENTIALS to a service account JSON file."
        )

    service_account_file = Path(service_account_file_value).expanduser().resolve()
    if not service_account_file.exists():
        raise RuntimeError(f"Service account file was not found: {service_account_file}")

    prefix = os.getenv("AMSF_GOOGLE_DRIVE_BACKUP_PREFIX", default_prefix).strip() or default_prefix
    keep_last_value = keep_last_override if keep_last_override is not None else int(
        os.getenv("AMSF_GOOGLE_DRIVE_KEEP_LAST", "7")
    )
    if keep_last_value < 1:
        raise RuntimeError("AMSF_GOOGLE_DRIVE_KEEP_LAST must be at least 1.")

    return DriveBackupConfig(
        folder_id=folder_id,
        service_account_file=service_account_file,
        prefix=prefix,
        keep_last=keep_last_value,
    )


def drive_service(config: DriveBackupConfig):
    credentials = service_account.Credentials.from_service_account_file(
        str(config.service_account_file),
        scopes=[GOOGLE_DRIVE_SCOPE],
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def list_backup_files(service, folder_id: str, prefix: str) -> list[dict]:
    escaped_prefix = prefix.replace("'", "\\'")
    query = f"'{folder_id}' in parents and trashed = false and name contains '{escaped_prefix}'"
    response = (
        service.files()
        .list(
            q=query,
            fields="files(id, name, createdTime)",
            orderBy="createdTime desc",
            pageSize=1000,
        )
        .execute()
    )
    return list(response.get("files", []))


def upload_backup(service, config: DriveBackupConfig, backup_path: Path) -> dict:
    media = MediaFileUpload(str(backup_path), mimetype="application/x-sqlite3", resumable=True)
    metadata = {
        "name": backup_path.name,
        "parents": [config.folder_id],
    }
    return (
        service.files()
        .create(body=metadata, media_body=media, fields="id, name, createdTime")
        .execute()
    )


def prune_old_backups(service, folder_id: str, prefix: str, keep_last: int, keep_file_id: str) -> list[str]:
    files = list_backup_files(service, folder_id, prefix)
    deleted = []
    for index, file_info in enumerate(files):
        if index < keep_last or file_info["id"] == keep_file_id:
            continue
        service.files().delete(fileId=file_info["id"]).execute()
        deleted.append(file_info["name"])
    return deleted


def main() -> None:
    parser = argparse.ArgumentParser(description="Back up the AMSF SQLite database to Google Drive.")
    parser.add_argument(
        "--keep-last",
        type=int,
        default=None,
        help="Override AMSF_GOOGLE_DRIVE_KEEP_LAST for this run.",
    )
    args = parser.parse_args()

    path = database_path()
    if not path.exists():
        raise RuntimeError(f"Database was not found: {path}")

    config = load_config(f"{path.stem}-", args.keep_last)
    local_backup = create_backup(path)

    service = drive_service(config)
    uploaded = upload_backup(service, config, local_backup)
    deleted = prune_old_backups(service, config.folder_id, config.prefix, config.keep_last, uploaded["id"])

    print(f"Uploaded Google Drive backup: {uploaded['name']} ({uploaded['id']})")
    print(f"Local backup: {local_backup}")
    if deleted:
        print("Deleted old Drive backups:")
        for name in deleted:
            print(f"- {name}")


if __name__ == "__main__":
    main()