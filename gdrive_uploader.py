import io
import json
import os
import re
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]


def _safe_name(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", "-", name or "Untitled")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120] or "Untitled"


def _credentials_info() -> dict:
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        return json.loads(raw)

    path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if path:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    raise RuntimeError("Set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE")


def drive_service():
    creds = service_account.Credentials.from_service_account_info(
        _credentials_info(), scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_or_create_folder(service, name: str, parent_id: Optional[str] = None) -> str:
    safe = _safe_name(name).replace("'", "\\'")
    query = (
        "mimeType='application/vnd.google-apps.folder' "
        f"and name='{safe}' and trashed=false"
    )
    if parent_id:
        query += f" and '{parent_id}' in parents"

    result = service.files().list(
        q=query,
        fields="files(id, name)",
        pageSize=1,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = result.get("files", [])
    if files:
        return files[0]["id"]

    metadata = {
        "name": _safe_name(name),
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        metadata["parents"] = [parent_id]

    folder = service.files().create(
        body=metadata,
        fields="id",
        supportsAllDrives=True,
    ).execute()
    return folder["id"]


def upload_bytes_to_drive(data: bytes, filename: str, folder_id: str) -> str:
    service = drive_service()
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype="application/zip", resumable=False)
    metadata = {"name": _safe_name(filename), "parents": [folder_id]}
    created = service.files().create(
        body=metadata,
        media_body=media,
        fields="id, webViewLink, webContentLink",
        supportsAllDrives=True,
    ).execute()

    file_id = created["id"]
    if os.getenv("GDRIVE_MAKE_PUBLIC", "1").strip() != "0":
        service.permissions().create(
            fileId=file_id,
            body={"role": "reader", "type": "anyone"},
            supportsAllDrives=True,
        ).execute()

    return created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"


def upload_chapter_zip(data: bytes, filename: str, manga_title: str) -> str:
    service = drive_service()
    root_id = os.getenv("GDRIVE_ROOT_FOLDER_ID", "").strip() or None
    folder_id = find_or_create_folder(service, manga_title, root_id)
    return upload_bytes_to_drive(data, filename, folder_id)
