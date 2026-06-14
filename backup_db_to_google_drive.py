from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

from migrate_db import create_backup, database_path

GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
GOOGLE_DRIVE_SCOPES = [GOOGLE_DRIVE_SCOPE]


@dataclass(frozen=True)
class DriveBackupConfig:
    folder_id: str
    oauth_client_file: Path
    oauth_token_file: Path
    prefix: str
    keep_last: int


def load_config(default_prefix: str, keep_last_override: int | None = None) -> DriveBackupConfig:
    folder_id = os.getenv("AMSF_GOOGLE_DRIVE_FOLDER_ID", "").strip()
    if not folder_id:
        raise RuntimeError("AMSF_GOOGLE_DRIVE_FOLDER_ID is required.")

    oauth_client_file_value = os.getenv("AMSF_GOOGLE_OAUTH_CLIENT_FILE", "").strip()
    if not oauth_client_file_value:
        raise RuntimeError("AMSF_GOOGLE_OAUTH_CLIENT_FILE is required.")

    oauth_token_file_value = os.getenv("AMSF_GOOGLE_OAUTH_TOKEN_FILE", "").strip()
    if not oauth_token_file_value:
        raise RuntimeError("AMSF_GOOGLE_OAUTH_TOKEN_FILE is required.")

    oauth_client_file = Path(oauth_client_file_value).expanduser().resolve()
    if not oauth_client_file.exists():
        raise RuntimeError(f"OAuth client file was not found: {oauth_client_file}")

    oauth_token_file = Path(oauth_token_file_value).expanduser().resolve()

    prefix = os.getenv("AMSF_GOOGLE_DRIVE_BACKUP_PREFIX", default_prefix).strip() or default_prefix
    keep_last_value = keep_last_override if keep_last_override is not None else int(
        os.getenv("AMSF_GOOGLE_DRIVE_KEEP_LAST", "7")
    )
    if keep_last_value < 1:
        raise RuntimeError("AMSF_GOOGLE_DRIVE_KEEP_LAST must be at least 1.")

    return DriveBackupConfig(
        folder_id=folder_id,
        oauth_client_file=oauth_client_file,
        oauth_token_file=oauth_token_file,
        prefix=prefix,
        keep_last=keep_last_value,
    )


def save_oauth_credentials(credentials: Credentials, token_file: Path) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(credentials.to_json(), encoding="utf-8")


def load_oauth_credentials(config: DriveBackupConfig, authorize: bool = False) -> Credentials:
    credentials: Credentials | None = None
    if config.oauth_token_file.exists():
        credentials = Credentials.from_authorized_user_file(str(config.oauth_token_file), GOOGLE_DRIVE_SCOPES)

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        save_oauth_credentials(credentials, config.oauth_token_file)
        return credentials

    if credentials and credentials.valid:
        return credentials

    if authorize:
        flow = InstalledAppFlow.from_client_secrets_file(str(config.oauth_client_file), GOOGLE_DRIVE_SCOPES)
        credentials = flow.run_local_server(port=0)
        save_oauth_credentials(credentials, config.oauth_token_file)
        return credentials

    raise RuntimeError(
        "Google OAuth token is missing or expired. Run with --authorize on a machine with a browser to create "
        f"{config.oauth_token_file}, then copy that token file to the server."
    )


def drive_service(credentials: Credentials):
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def validate_backup_folder(service, folder_id: str) -> dict:
    try:
        return service.files().get(fileId=folder_id, fields="id, name, mimeType", supportsAllDrives=True).execute()
    except HttpError as exc:
        if exc.resp.status == 404:
            raise RuntimeError(
                "Google Drive folder not found or not shared with the service account. "
                "Check AMSF_GOOGLE_DRIVE_FOLDER_ID and share the folder with the service account email."
            ) from exc
        raise


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
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
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
        .create(body=metadata, media_body=media, fields="id, name, createdTime", supportsAllDrives=True)
        .execute()
    )


def prune_old_backups(service, folder_id: str, prefix: str, keep_last: int, keep_file_id: str) -> list[str]:
    files = list_backup_files(service, folder_id, prefix)
    deleted = []
    for index, file_info in enumerate(files):
        if index < keep_last or file_info["id"] == keep_file_id:
            continue
        service.files().delete(fileId=file_info["id"], supportsAllDrives=True).execute()
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
    parser.add_argument(
        "--authorize",
        action="store_true",
        help="Open a browser login flow and save a Google OAuth refresh token.",
    )
    args = parser.parse_args()

    path = database_path()
    if not path.exists():
        raise RuntimeError(f"Database was not found: {path}")

    config = load_config(f"{path.stem}-", args.keep_last)
    local_backup = create_backup(path)

    credentials = load_oauth_credentials(config, authorize=args.authorize)
    service = drive_service(credentials)
    folder = validate_backup_folder(service, config.folder_id)
    uploaded = upload_backup(service, config, local_backup)
    deleted = prune_old_backups(service, config.folder_id, config.prefix, config.keep_last, uploaded["id"])

    print(f"Uploaded Google Drive backup to {folder['name']}: {uploaded['name']} ({uploaded['id']})")
    print(f"Local backup: {local_backup}")
    if deleted:
        print("Deleted old Drive backups:")
        for name in deleted:
            print(f"- {name}")


if __name__ == "__main__":
    main()