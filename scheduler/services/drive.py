from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path

from django.conf import settings


FOLDER_PATTERNS = [
    re.compile(r"/folders/([a-zA-Z0-9_-]+)"),
    re.compile(r"[?&]id=([a-zA-Z0-9_-]+)"),
]


class DriveConfigError(Exception):
    pass


def extract_drive_folder_id(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    for pattern in FOLDER_PATTERNS:
        match = pattern.search(value)
        if match:
            return match.group(1)
    return value


def get_drive_service():
    service_account_file = settings.GOOGLE_SERVICE_ACCOUNT_FILE
    if not service_account_file:
        raise DriveConfigError("GOOGLE_SERVICE_ACCOUNT_FILE is not configured.")
    if not Path(service_account_file).exists():
        raise DriveConfigError("Configured Google service account file does not exist.")

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    credentials = service_account.Credentials.from_service_account_file(
        service_account_file,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def get_drive_file_metadata(file_id: str) -> dict:
    service = get_drive_service()
    return (
        service.files()
        .get(
            fileId=file_id,
            fields="id,name,mimeType,size",
            supportsAllDrives=True,
        )
        .execute()
    )


def download_drive_file(file_id: str) -> bytes:
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    from googleapiclient.http import MediaIoBaseDownload

    buffer = BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buffer.getvalue()


def list_folder_files(folder_id: str) -> list[dict]:
    folder_id = extract_drive_folder_id(folder_id)
    if not folder_id:
        raise DriveConfigError("Google Drive folder ID is required.")

    service = get_drive_service()
    files = []
    page_token = None
    while True:
        response = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken,files(id,name,mimeType,createdTime,webViewLink,webContentLink)",
                orderBy="createdTime",
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return files


def is_publishable_media(file_obj: dict) -> bool:
    mime_type = file_obj.get("mimeType", "")
    return mime_type.startswith("image/") or mime_type.startswith("video/")


def find_caption_file(files: list[dict]) -> dict | None:
    for file_obj in files:
        if file_obj.get("name", "").lower() == "caption.txt":
            return file_obj
    return None


def ensure_public_file(service, file_id: str):
    service.permissions().create(fileId=file_id, body={"type": "anyone", "role": "reader"}).execute()


def get_public_media_urls(file_obj: dict) -> list[str]:
    file_id = file_obj.get("id", "")
    if not file_id:
        raise DriveConfigError("Drive file is missing an ID.")
    urls = [
        f"https://drive.googleusercontent.com/uc?id={file_id}&export=download",
        file_obj.get("webContentLink", ""),
        f"https://drive.google.com/uc?export=download&id={file_id}",
    ]
    return [url for url in urls if url]


def get_publishable_file_url(file_obj: dict) -> str:
    file_id = file_obj.get("id", "")
    if not file_id:
        raise DriveConfigError("Drive file is missing an ID.")
    return get_public_media_urls(file_obj)[0]
