# routes/personal_routes.py
"""Routes for personal documents management."""
import os
import logging
import uuid
import inspect
from typing import List, Tuple
from fastapi import APIRouter, HTTPException, Query, Request, UploadFile, File, Depends
from src.request_models import DirectoryRequest
from core.constants import DATA_DIR, PERSONAL_DIR
from src.rag_singleton import get_rag_manager
from src.auth_helpers import require_user
from core.middleware import require_admin
from src.personal_paths import (
    path_visible_to_personal_owner,
    personal_upload_dir_for_owner as scoped_personal_upload_dir_for_owner,
    resolve_owned_personal_file,
)
from src.upload_handler import secure_filename

UPLOADS_DIR = os.path.join(DATA_DIR, "personal_uploads")
MAX_PERSONAL_UPLOAD_BYTES = int(
    os.getenv("ODYSSEUS_PERSONAL_UPLOAD_MAX_BYTES", str(25 * 1024 * 1024))
)

logger = logging.getLogger(__name__)


def _personal_upload_dir_for_owner(owner: str | None) -> str:
    """Return the per-owner upload directory used for direct RAG uploads."""
    return scoped_personal_upload_dir_for_owner(owner, UPLOADS_DIR)


def _unique_personal_upload_path(upload_dir: str, original_name: str | None) -> Tuple[str, str, str]:
    """Build a collision-resistant upload path while preserving a display name."""
    safe_name = secure_filename(os.path.basename(original_name or "upload"))
    if not safe_name or safe_name.startswith("."):
        safe_name = "upload"

    stem, ext = os.path.splitext(safe_name)
    stem = (stem or "upload")[:80]
    filename = f"{stem}-{uuid.uuid4().hex[:10]}{ext.lower()}"
    file_path = os.path.abspath(os.path.join(upload_dir, filename))
    upload_abs = os.path.abspath(upload_dir)
    if os.path.commonpath([file_path, upload_abs]) != upload_abs:
        raise ValueError("Unsafe upload filename")
    return file_path, filename, safe_name

def setup_personal_routes(personal_docs_manager, rag_manager, rag_available):
    """
    Setup personal documents related routes.

    Args:
        personal_docs_manager: PersonalDocsManager instance
        rag_manager: RAG manager instance (may be None)
        rag_available: Boolean indicating if RAG is available

    Returns:
        APIRouter instance with personal docs routes
    """
    router = APIRouter(prefix="/api/personal")

    def _rag():
        """Get the current RAG manager, retrying init if needed."""
        return get_rag_manager()

    def _resolve_allowed_personal_dir(directory: str) -> str:
        """Resolve a user-supplied personal-docs path under the allowed root."""
        if not directory:
            raise HTTPException(400, "Directory path is required")

        # realpath (not abspath) so a symlink inside PERSONAL_DIR that points
        # outside it is resolved before the commonpath confinement check below;
        # abspath only normalises `..` and would let such a symlink escape.
        base_abs = os.path.realpath(PERSONAL_DIR)
        candidate = directory if os.path.isabs(directory) else os.path.join(base_abs, directory)
        resolved = os.path.realpath(candidate)
        try:
            in_base = os.path.commonpath([resolved, base_abs]) == base_abs
        except ValueError:
            in_base = False
        if not in_base:
            raise HTTPException(403, "Directory must be inside personal documents")
        return resolved

    def _path_visible_to_owner(path: str, owner: str | None) -> bool:
        return path_visible_to_personal_owner(
            path,
            owner,
            uploads_dir=UPLOADS_DIR,
            personal_dir=PERSONAL_DIR,
        )

    def _visible_files(owner: str | None):
        files = []
        for f in personal_docs_manager.index:
            if not isinstance(f, dict):
                continue
            entry_owner = (f.get("owner") or "").strip()
            if entry_owner and entry_owner != (owner or ""):
                continue
            path = f.get("path", "")
            if not _path_visible_to_owner(path, owner):
                continue
            files.append({"name": f["name"], "size": f["size"], "path": path})
        return files

    def _visible_directories(owner: str | None):
        if not hasattr(personal_docs_manager, "get_indexed_directories"):
            return []
        return [
            d for d in personal_docs_manager.get_indexed_directories()
            if isinstance(d, str) and _path_visible_to_owner(d, owner)
        ]

    def _resolve_removable_directory(directory: str, owner: str | None) -> str:
        try:
            return _resolve_allowed_personal_dir(directory)
        except HTTPException as personal_exc:
            upload_dir = _personal_upload_dir_for_owner(owner)
            candidate = directory if os.path.isabs(directory) else os.path.join(upload_dir, directory)
            resolved = os.path.realpath(candidate)
            if _path_visible_to_owner(resolved, owner) and os.path.commonpath(
                [resolved, os.path.realpath(upload_dir)]
            ) == os.path.realpath(upload_dir):
                return resolved
            raise HTTPException(403, "Directory must be inside personal documents or your uploads") from personal_exc

    def _manager_remove_directory(directory: str, owner: str | None):
        if owner:
            params = inspect.signature(personal_docs_manager.remove_directory).parameters.values()
            if not any(p.kind == inspect.Parameter.VAR_KEYWORD or p.name == "owner" for p in params):
                raise RuntimeError("owner-scoped personal document removal is unavailable")
            personal_docs_manager.remove_directory(directory, owner=owner)
            return
        try:
            personal_docs_manager.remove_directory(directory, owner=owner)
        except TypeError:
            personal_docs_manager.remove_directory(directory)

    def _rag_remove_directory(rag, directory: str, owner: str | None):
        if owner:
            params = inspect.signature(rag.remove_directory).parameters.values()
            if not any(p.kind == inspect.Parameter.VAR_KEYWORD or p.name == "owner" for p in params):
                raise RuntimeError("owner-scoped RAG directory removal is unavailable")
            return rag.remove_directory(directory, owner=owner)
        try:
            return rag.remove_directory(directory, owner=owner)
        except TypeError:
            return rag.remove_directory(directory)

    def _rag_delete_by_source(rag, filepath: str, owner: str | None):
        if owner:
            params = inspect.signature(rag.delete_by_source).parameters.values()
            if not any(p.kind == inspect.Parameter.VAR_KEYWORD or p.name == "owner" for p in params):
                raise RuntimeError("owner-scoped RAG source deletion is unavailable")
            return rag.delete_by_source(filepath, owner=owner)
        try:
            return rag.delete_by_source(filepath, owner=owner)
        except TypeError:
            return rag.delete_by_source(filepath)
    
    @router.get("")
    def api_personal_list(owner: str = Depends(require_user), _admin: None = Depends(require_admin)):
        """Enhanced version that includes directories"""
        files = _visible_files(owner)
        directories = _visible_directories(owner)
        return {"files": files, "directories": directories}
    
    @router.post("/reload")
    def api_personal_reload(owner: str = Depends(require_user), _admin: None = Depends(require_admin)):
        personal_docs_manager.refresh_index()
        return {"ok": True, "count": len(_visible_files(owner))}
    
    @router.post("/add_directory")
    async def add_directory_to_rag(
        request: Request,
        directory_request: DirectoryRequest,
        owner: str = Depends(require_user), _admin: None = Depends(require_admin),
    ):
        """
        Add a directory and all its subdirectories/files to the RAG index.
        
        Args:
            directory_request: Directory request model containing the directory path
            
        Returns:
            JSON response with indexing results
        """
        directory = directory_request.directory
        try:
            directory = _resolve_allowed_personal_dir(directory)
            
            # Security check - ensure directory exists and is accessible
            if not os.path.exists(directory):
                raise HTTPException(404, f"Directory not found: {directory}")
            
            if not os.path.isdir(directory):
                raise HTTPException(400, f"Path is not a directory: {directory}")
            
            logger.info(f"Adding directory to RAG: {directory}")
            
            # Use the RAGManager to index the directory
            rag = _rag()
            if rag:
                result = rag.index_personal_documents(directory, owner=owner)
                
                if result["success"]:
                    # Also update the personal_docs_manager to track this directory
                    personal_docs_manager.add_directory(directory, index=False, owner=owner)
                    
                    return {
                        "success": True,
                        "message": f"Successfully indexed {result['indexed_count']} chunks from {directory}",
                        "indexed_count": result["indexed_count"],
                        "failed_count": result.get("failed_count", 0),
                        "directory": directory
                    }
                else:
                    raise HTTPException(500, result.get("message", "Failed to index directory"))
            else:
                raise HTTPException(503, "RAG system is not available")
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error adding directory to RAG: {e}")
            raise HTTPException(500, f"Failed to add directory: {str(e)}")
    
    @router.delete("/remove_directory")
    async def remove_directory_from_rag(directory: str = Query(...), owner: str = Depends(require_user), _admin: None = Depends(require_admin)):
        """
        Remove a directory from the RAG index.

        Args:
            directory: Path to the directory to remove

        Returns:
            JSON response confirming removal
        """
        try:
            if not directory:
                raise HTTPException(400, "Directory path is required")
            directory = _resolve_removable_directory(directory, owner)

            logger.info(f"Removing directory from RAG: {directory}")

            # Always remove from personal_docs_manager tracking
            if hasattr(personal_docs_manager, 'remove_directory'):
                _manager_remove_directory(directory, owner)

            # Remove from RAG vector store (best-effort)
            rag = _rag()
            if rag:
                try:
                    _rag_remove_directory(rag, directory, owner)
                except Exception as e:
                    logger.warning(f"RAG removal failed for directory {directory}: {e}")

            return {
                "success": True,
                "message": f"Successfully removed {directory} from RAG index",
                "directory": directory
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error removing directory from RAG: {e}")
            raise HTTPException(500, f"Failed to remove directory: {str(e)}")
    
    @router.post("/upload")
    async def upload_files_to_rag(request: Request, files: List[UploadFile] = File(...)):
        """Upload files directly into RAG. Supports text and PDF."""
        user = require_user(request)
        rag = _rag()
        if not rag:
            raise HTTPException(503, "RAG system is not available — is the embedding service running?")

        upload_dir = _personal_upload_dir_for_owner(user)

        total_indexed = 0
        total_failed = 0
        uploaded_files = []

        for upload in files:
            try:
                file_path, stored_name, safe_name = _unique_personal_upload_path(upload_dir, upload.filename)
                content_bytes = await upload.read(MAX_PERSONAL_UPLOAD_BYTES + 1)
                if len(content_bytes) > MAX_PERSONAL_UPLOAD_BYTES:
                    logger.warning(f"Rejected oversized personal upload: {upload.filename!r}")
                    total_failed += 1
                    continue
                with open(file_path, "wb") as f:
                    f.write(content_bytes)

                ext = os.path.splitext(safe_name)[1].lower()
                if ext == ".pdf":
                    from src.personal_docs import extract_pdf_text
                    text = extract_pdf_text(file_path)
                else:
                    text = content_bytes.decode("utf-8", errors="replace")

                if not text or not text.strip():
                    total_failed += 1
                    continue

                # Chunk and index
                chunks = rag._split_into_chunks(text, chunk_size=500)
                for i, chunk in enumerate(chunks):
                    metadata = {
                        "source": file_path,
                        "filename": safe_name,
                        "stored_filename": stored_name,
                        "directory": upload_dir,
                        "type": ext,
                        "chunk_id": i,
                    }
                    if user:
                        metadata["owner"] = user
                    if rag.add_document(chunk, metadata):
                        total_indexed += 1
                    else:
                        total_failed += 1

                uploaded_files.append(safe_name)
            except Exception as e:
                logger.error(f"Failed to upload/index {upload.filename}: {e}")
                total_failed += 1

        # Track uploads directory
        if uploaded_files and hasattr(personal_docs_manager, "add_directory"):
            personal_docs_manager.add_directory(upload_dir, index=False)

        return {
            "success": True,
            "uploaded": uploaded_files,
            "indexed_count": total_indexed,
            "failed_count": total_failed,
        }

    @router.delete("/file")
    async def delete_file_from_rag(filepath: str = Query(...), owner: str = Depends(require_user), _admin: None = Depends(require_admin)):
        """Delete a specific file from RAG index and optionally from disk."""
        try:
            try:
                filepath = resolve_owned_personal_file(
                    filepath,
                    owner,
                    uploads_dir=UPLOADS_DIR,
                    personal_dir=PERSONAL_DIR,
                )
            except ValueError as exc:
                raise HTTPException(403, str(exc)) from exc

            # Remove chunks from RAG vector store (best-effort)
            removed = 0
            rag = _rag()
            if rag:
                try:
                    removed = _rag_delete_by_source(rag, filepath, owner)
                except Exception as e:
                    logger.warning(f"RAG removal failed for {filepath}: {e}")

            # Delete file from disk if it's in uploads dir
            deleted_from_disk = False
            try:
                abs_target = os.path.abspath(filepath)
                base_abs = os.path.abspath(_personal_upload_dir_for_owner(owner))
                in_uploads = (
                    abs_target == base_abs
                    or os.path.commonpath([abs_target, base_abs]) == base_abs
                )
            except ValueError:
                # commonpath raises on mixed drives / non-comparable paths
                in_uploads = False
            if in_uploads and abs_target != base_abs and os.path.exists(abs_target):
                os.remove(abs_target)
                deleted_from_disk = True

            # Exclude the file from the listing (persists across restarts)
            personal_docs_manager.exclude_file(filepath)

            return {
                "success": True,
                "removed_chunks": removed,
                "deleted_from_disk": deleted_from_disk,
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to delete file {filepath}: {e}")
            raise HTTPException(500, f"Failed to delete file: {str(e)}")

    return router
