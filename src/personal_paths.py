"""Path confinement helpers for personal/RAG document storage."""

from __future__ import annotations

import os
import hashlib

from core.constants import DATA_DIR, PERSONAL_DIR


DEFAULT_PERSONAL_UPLOADS_DIR = os.path.join(DATA_DIR, "personal_uploads")


def personal_owner_segment(owner: str | None) -> str:
    key = (owner or "").strip().lower()
    if not key:
        return "local"
    return f"owner-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}"


def personal_upload_dir_for_owner(
    owner: str | None,
    uploads_dir: str = DEFAULT_PERSONAL_UPLOADS_DIR,
    *,
    create: bool = True,
) -> str:
    """Return the confined upload directory for an owner."""
    base_abs = os.path.abspath(uploads_dir)
    upload_dir = os.path.abspath(os.path.join(base_abs, personal_owner_segment(owner)))
    if os.path.commonpath([upload_dir, base_abs]) != base_abs:
        raise ValueError("Unsafe upload owner path")
    if create:
        os.makedirs(upload_dir, exist_ok=True)
    return upload_dir


def path_inside(path: str, base: str, *, resolve_symlinks: bool = True) -> bool:
    """Return whether path resolves to base or a descendant of base."""
    normalizer = os.path.realpath if resolve_symlinks else os.path.abspath
    try:
        path_abs = normalizer(path)
        base_abs = normalizer(base)
        return path_abs == base_abs or os.path.commonpath([path_abs, base_abs]) == base_abs
    except (TypeError, ValueError):
        return False


def resolve_personal_dir(directory: str, *, personal_dir: str = PERSONAL_DIR) -> str:
    """Resolve a directory path under the personal-documents root."""
    if not directory:
        raise ValueError("Directory path is required")
    base_abs = os.path.realpath(personal_dir)
    candidate = directory if os.path.isabs(directory) else os.path.join(base_abs, directory)
    resolved = os.path.realpath(candidate)
    if not path_inside(resolved, base_abs):
        raise ValueError("Directory must be inside personal documents")
    return resolved


def path_visible_to_personal_owner(
    path: str,
    owner: str | None,
    *,
    uploads_dir: str = DEFAULT_PERSONAL_UPLOADS_DIR,
    personal_dir: str = PERSONAL_DIR,
) -> bool:
    """Allow shared personal docs plus the caller's own upload directory."""
    if not path:
        return False
    if path_inside(path, personal_dir):
        return True
    owner_upload_dir = personal_upload_dir_for_owner(owner, uploads_dir, create=False)
    return path_inside(path, owner_upload_dir)


def resolve_owned_personal_file(
    filepath: str,
    owner: str | None,
    *,
    uploads_dir: str = DEFAULT_PERSONAL_UPLOADS_DIR,
    personal_dir: str = PERSONAL_DIR,
) -> str:
    """Resolve a file path the owner may delete/exclude from personal RAG."""
    if not filepath:
        raise ValueError("File path is required")
    owner_upload_dir = personal_upload_dir_for_owner(owner, uploads_dir, create=False)
    candidate = filepath if os.path.isabs(filepath) else os.path.join(owner_upload_dir, filepath)
    resolved = os.path.realpath(candidate)
    if not (
        path_inside(resolved, owner_upload_dir)
        or path_inside(resolved, personal_dir)
    ):
        raise ValueError("File must be inside your personal uploads or personal documents")
    if resolved in {os.path.realpath(owner_upload_dir), os.path.realpath(personal_dir)}:
        raise ValueError("File path is required")
    return resolved
