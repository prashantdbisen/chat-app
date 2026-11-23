"""
storage.py - Saving shared files safely.

Safety rules (good interview talking points):
  * Files are saved under a RANDOM name, never the uploader's name, so nobody can
    overwrite files or use tricks like "../../" to write outside the folder.
  * Size is checked WHILE streaming to disk, so a huge upload is stopped early
    instead of filling memory.
  * Only well-known photo/video/audio formats are shown inside the chat. Everything
    else is sent as a download, so an uploaded HTML/SVG file can't run scripts.
  * Photos are checked by their first bytes ("magic numbers"), not just the name.
  * Executable files (.exe, .bat, ...) are refused.
"""

import os
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from app.config import MAX_UPLOAD_MB, UPLOAD_DIR

CHUNK_SIZE = 1024 * 1024  # 1 MB at a time

# Formats that are safe to display inside the page, by kind
INLINE_TYPES = {
    "image": {"image/jpeg", "image/png", "image/gif", "image/webp"},
    "video": {"video/mp4", "video/webm", "video/quicktime", "video/ogg"},
    "audio": {"audio/mpeg", "audio/ogg", "audio/wav", "audio/x-wav", "audio/webm",
              "audio/mp4", "audio/aac", "audio/x-m4a"},
}
INLINE_CONTENT_TYPES = set().union(*INLINE_TYPES.values()) | {"application/pdf"}

BLOCKED_EXTENSIONS = {".exe", ".bat", ".cmd", ".com", ".msi", ".scr", ".ps1", ".vbs",
                      ".js", ".jar", ".sh", ".dll", ".hta", ".lnk"}

_IMAGE_SIGNATURES = {
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/png": [b"\x89PNG\r\n\x1a\n"],
    "image/gif": [b"GIF87a", b"GIF89a"],
    "image/webp": [b"RIFF"],  # followed by "WEBP" at byte 8
}


def upload_dir() -> Path:
    path = Path(UPLOAD_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _clean_name(name: str) -> str:
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    return name[:200] or "file"


def _base_type(content_type: str | None) -> str:
    return (content_type or "").split(";")[0].strip().lower()  # "audio/webm;codecs=opus" -> "audio/webm"


def _looks_like_image(first_bytes: bytes, content_type: str) -> bool:
    signatures = _IMAGE_SIGNATURES.get(content_type, [])
    if content_type == "image/webp":
        return first_bytes.startswith(b"RIFF") and first_bytes[8:12] == b"WEBP"
    return any(first_bytes.startswith(sig) for sig in signatures)


async def save_upload(file: UploadFile) -> dict:
    """Stream an upload to disk. Returns its details, or raises an HTTP error."""
    original_name = _clean_name(file.filename)
    extension = Path(original_name).suffix.lower()
    if extension in BLOCKED_EXTENSIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "For safety, program files like this can't be shared.")

    content_type = _base_type(file.content_type) or "application/octet-stream"
    stored_name = f"{uuid.uuid4().hex}{extension[:10]}"
    path = upload_dir() / stored_name
    limit = MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    first_bytes = b""

    try:
        with open(path, "wb") as output:
            while chunk := await file.read(CHUNK_SIZE):
                if not first_bytes:
                    first_bytes = chunk[:16]
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413,  # "Content too large"
                                        f"Files can be up to {MAX_UPLOAD_MB} MB.")
                output.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)  # Don't leave half-written files behind
        raise

    if size == 0:
        path.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That file is empty.")

    kind = next((k for k, types in INLINE_TYPES.items() if content_type in types), "file")
    if kind == "image" and not _looks_like_image(first_bytes, content_type):
        kind = "file"  # Claims to be a photo but isn't - treat it as a plain download

    return {"stored_name": stored_name, "original_name": original_name,
            "content_type": content_type, "kind": kind, "size": size}


def file_path(stored_name: str) -> Path:
    return upload_dir() / stored_name


def delete_file(stored_name: str) -> None:
    file_path(stored_name).unlink(missing_ok=True)
