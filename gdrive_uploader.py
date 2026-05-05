import io
import json
import os
import re
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]


def safe_name(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|]+", "-", value or "Untitled")
    value = re.sub(r"\s+", " ", value).strip()
    return value[:120] or "Untitled"


def load_token() -> dict:
    raw = os.getenv("GOOGLE_OAUTH_TOKEN_JSON", "").strip()
    if raw:
        return json.loads(raw)
    path = os.getenv("GOOGLE_OAUTH_TOKEN_FILE", "").strip()
    if path:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    raise RuntimeError("Set GOOGLE_OAUTH_TOKEN_JSON")


def drive_service():
    creds = Credentials.from_authorized_user_info(load_token(), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_or_create_folder(service, name: str, parent_id: Optional[str] = None) -> str:
    folder_name = safe_name(name)
    q_name = folder_name.replace("'", "\\'")
    query = "mimeType='application/vnd.google-apps.folder' and trashed=false and name='%s'" % q_name
    if parent_id:
        query += " and '%s' in parents" % parent_id
    result = service.files().list(q=query, fields="files(id,name)", pageSize=1, supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    files = result.get("files", [])
    if files:
        return files[0]["id"]
    body = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        body["parents"] = [parent_id]
    created = service.files().create(body=body, fields="id", supportsAllDrives=True).execute()
    return created["id"]


def upload_chapter_zip(data: bytes, filename: str, manga_title: str) -> str:
    service = drive_service()
    root_id = os.getenv("GDRIVE_ROOT_FOLDER_ID", "").strip() or None
    folder_id = find_or_create_folder(service, manga_title, root_id)
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype="application/zip", resumable=False)
    body = {"name": safe_name(filename), "parents": [folder_id]}
    created = service.files().create(body=body, media_body=media, fields="id,webViewLink", supportsAllDrives=True).execute()
    file_id = created["id"]
    if os.getenv("GDRIVE_MAKE_PUBLIC", "1").strip() != "0":
        service.permissions().create(fileId=file_id, body={"role": "reader", "type": "anyone"}, supportsAllDrives=True).execute()
    return created.get("webViewLink") or "https://drive.google.com/file/d/%s/view" % file_id
