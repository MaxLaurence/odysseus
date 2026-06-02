"""Code Station git-worktree child-spaces (herdr ``worktree create/open/remove``).

A Space is a :class:`~core.database.CodingProject` pointing at a git repo. This
module lets a root Space spawn *worktree child-spaces*: a new branch checked out
in a separate directory, registered as its own ``CodingProject`` row with
``kind == 'worktree'`` and ``parent_project_id`` pointing back at the root. The
UI nests these children under their parent.

It mirrors how the other Code Station seams are structured: owner-scoped service
methods raise :class:`~src.coding_runtime.CodingRuntimeError` and return the
canonical space dict via
:meth:`~src.coding_workspace.CodingWorkspaceService.space_dict`, so REST routes,
the socket JSON-RPC server, and the in-process provider tool can all call one
impl. Git is driven through ``git -C <root> worktree …`` subprocesses.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

from core.database import CodingProject, SessionLocal
from src.coding_runtime import CodingRuntimeError
from src.coding_workspace import get_coding_workspace_service

logger = logging.getLogger(__name__)

# Reasonable ceilings — `worktree add` can clone/checkout a large tree.
_GIT_QUICK_TIMEOUT = 5.0
_GIT_ADD_TIMEOUT = 120.0
_GIT_REMOVE_TIMEOUT = 60.0


def _worktrees_root() -> Path:
    """Base dir for generated worktree checkouts (``ODYSSEUS_WORKTREES_DIR``)."""
    raw = os.environ.get("ODYSSEUS_WORKTREES_DIR") or "~/.odysseus/worktrees"
    return Path(raw).expanduser()


def _sanitize_branch(branch: str) -> str:
    """Make a branch name safe to use as a single filesystem path segment."""
    cleaned = branch.strip().strip("/")
    # '/' is the namespace separator in git branch names; flatten to '-'.
    cleaned = cleaned.replace("/", "-")
    # Drop anything else that's awkward on a filesystem.
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", cleaned)
    cleaned = cleaned.strip("-.") or "worktree"
    return cleaned


def _repo_name(root_path: str) -> str:
    name = Path(root_path).name or "repo"
    return _sanitize_branch(name)


def _is_git_repo(root_path: str | None) -> bool:
    if not root_path or not Path(root_path).exists():
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", root_path, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=_GIT_QUICK_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("git rev-parse failed for %s: %s", root_path, exc)
        return False
    return proc.returncode == 0 and (proc.stdout or "").strip() == "true"


def _branch_exists(root_path: str, branch: str) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", root_path, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            capture_output=True, text=True, timeout=_GIT_QUICK_TIMEOUT,
        )
    except Exception:  # noqa: BLE001
        return False
    return proc.returncode == 0


class CodingWorktreeService:
    """Owner-scoped git-worktree child-space CRUD for Code Station."""

    def __init__(self) -> None:
        self._workspace = get_coding_workspace_service()

    # --------------------------------------------------------------- helpers
    def _require_space(self, db, owner: str, space_id: str) -> CodingProject:
        project = (
            db.query(CodingProject)
            .filter(CodingProject.owner == owner, CodingProject.id == space_id)
            .first()
        )
        if not project:
            raise CodingRuntimeError(404, "Space not found")
        return project

    def _require_worktree(self, db, owner: str, worktree_space_id: str) -> CodingProject:
        project = (
            db.query(CodingProject)
            .filter(CodingProject.owner == owner, CodingProject.id == worktree_space_id)
            .first()
        )
        if not project:
            raise CodingRuntimeError(404, "Worktree space not found")
        if (project.kind or "root") != "worktree":
            raise CodingRuntimeError(400, "Space is not a worktree child-space")
        return project

    def _parent_root(self, db, child: CodingProject) -> str | None:
        """Resolve the git repo root that owns the worktree (the parent space)."""
        if child.parent_project_id:
            parent = (
                db.query(CodingProject)
                .filter(CodingProject.id == child.parent_project_id)
                .first()
            )
            if parent and parent.root_path:
                return parent.root_path
        # Fall back to the child's own checkout — `git -C <worktree>` still talks
        # to the shared repo, so worktree remove/prune work from here too.
        return child.root_path

    # ----------------------------------------------------------------- list
    def list_worktrees(self, owner: str, space_id: str) -> list[dict[str, Any]]:
        """Child worktree spaces of ``space_id`` (kind=='worktree'), each as a
        canonical space dict. Best-effort reconciles against ``git worktree
        list`` so stale rows (dir removed out-of-band) are detectable, but never
        mutates state here."""
        db = SessionLocal()
        try:
            parent = self._require_space(db, owner, space_id)
            children = (
                db.query(CodingProject)
                .filter(
                    CodingProject.owner == owner,
                    CodingProject.parent_project_id == space_id,
                    CodingProject.kind == "worktree",
                )
                .order_by(CodingProject.created_at.asc())
                .all()
            )
            live_paths = self._git_worktree_paths(parent.root_path)
            result: list[dict[str, Any]] = []
            for child in children:
                space = self._workspace.space_dict(child)
                wt_path = child.worktree_path or child.root_path
                if live_paths is not None:
                    space["worktree_exists"] = (
                        self._normalize_path(wt_path) in live_paths
                        and bool(wt_path)
                        and Path(wt_path).exists()
                    )
                result.append(space)
            return result
        finally:
            db.close()

    @staticmethod
    def _normalize_path(path: str | None) -> str:
        if not path:
            return ""
        try:
            return str(Path(path).resolve())
        except Exception:  # noqa: BLE001
            return str(path)

    def _git_worktree_paths(self, root_path: str | None) -> set[str] | None:
        """Set of resolved worktree paths known to git, or None if unavailable."""
        if not _is_git_repo(root_path):
            return None
        try:
            proc = subprocess.run(
                ["git", "-C", root_path, "worktree", "list", "--porcelain"],
                capture_output=True, text=True, timeout=_GIT_QUICK_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("git worktree list failed for %s: %s", root_path, exc)
            return None
        if proc.returncode != 0:
            return None
        paths: set[str] = set()
        for line in (proc.stdout or "").splitlines():
            if line.startswith("worktree "):
                paths.add(self._normalize_path(line[len("worktree "):].strip()))
        return paths

    # --------------------------------------------------------------- create
    def create_worktree(self, owner: str, space_id: str, branch: str, *,
                        path: str | None = None, base: str | None = None) -> dict[str, Any]:
        """Add a git worktree for ``branch`` under the parent space and register
        it as a child ``CodingProject``. Returns the child's space dict."""
        branch = (branch or "").strip()
        if not branch:
            raise CodingRuntimeError(400, "Branch name is required")
        # Reject leading-dash values so they can't be parsed as git flags
        # (e.g. branch="--detach" / base="--force") in the positional `add` args.
        if branch.startswith("-"):
            raise CodingRuntimeError(400, "Invalid branch name")
        if base and str(base).startswith("-"):
            raise CodingRuntimeError(400, "Invalid base ref")

        db = SessionLocal()
        try:
            parent = self._require_space(db, owner, space_id)
            if not _is_git_repo(parent.root_path):
                raise CodingRuntimeError(400, "Space is not a git repository")

            # Compute the checkout path.
            if path:
                checkout = Path(path).expanduser()
            else:
                checkout = (
                    _worktrees_root()
                    / _repo_name(parent.root_path)
                    / _sanitize_branch(branch)
                )
            checkout_str = str(checkout)
            if checkout.exists():
                raise CodingRuntimeError(409, f"Worktree path already exists: {checkout_str}")
            checkout.parent.mkdir(parents=True, exist_ok=True)

            # New branch → `-b`; existing branch → check it out as-is.
            cmd = ["git", "-C", parent.root_path, "worktree", "add"]
            if _branch_exists(parent.root_path, branch):
                cmd += [checkout_str, branch]
            else:
                cmd += ["-b", branch, checkout_str]
                if base:
                    cmd.append(base)

            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=_GIT_ADD_TIMEOUT,
                )
            except subprocess.TimeoutExpired as exc:
                raise CodingRuntimeError(400, "git worktree add timed out") from exc
            except Exception as exc:  # noqa: BLE001
                raise CodingRuntimeError(400, f"git worktree add failed: {exc}") from exc

            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "git worktree add failed").strip()
                # Don't leave a half-created checkout behind (git may have populated
                # it before failing, which would block retry). Only recursively remove
                # a path WE computed under the worktrees root; never a caller path.
                if path is None:
                    import shutil
                    shutil.rmtree(checkout, ignore_errors=True)
                else:
                    self._safe_rmdir(checkout)
                raise CodingRuntimeError(400, detail)

            child = CodingProject(
                id=str(uuid.uuid4()),
                owner=owner,
                name=f"{parent.name} · {branch}",
                root_path=checkout_str,
                description="",
                default_harness=parent.default_harness or "generic",
                default_endpoint_id=parent.default_endpoint_id,
                default_model=parent.default_model,
                archived=False,
                parent_project_id=space_id,
                kind="worktree",
                worktree_branch=branch,
                worktree_path=checkout_str,
            )
            db.add(child)
            db.commit()
            db.refresh(child)
            logger.info(
                "Created worktree space %s (branch=%s) under %s at %s",
                child.id, branch, space_id, checkout_str,
            )
            return self._workspace.space_dict(child)
        finally:
            db.close()

    @staticmethod
    def _safe_rmdir(path: Path) -> None:
        """Remove an empty (just-created) dir; ignore if non-empty/missing."""
        try:
            if path.exists() and path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        except Exception:  # noqa: BLE001
            pass

    # ----------------------------------------------------------------- open
    def open_worktree(self, owner: str, worktree_space_id: str) -> dict[str, Any]:
        """Return the child worktree space dict (it's already a registered
        space). A focus convenience; 404 if missing."""
        db = SessionLocal()
        try:
            child = self._require_worktree(db, owner, worktree_space_id)
            return self._workspace.space_dict(child)
        finally:
            db.close()

    # --------------------------------------------------------------- remove
    def remove_worktree(self, owner: str, worktree_space_id: str, *,
                        force: bool = False) -> None:
        """Remove the git worktree and delete its child ``CodingProject`` row. If
        the worktree dir is already gone, still delete the row and prune git."""
        db = SessionLocal()
        try:
            child = self._require_worktree(db, owner, worktree_space_id)
            parent_root = self._parent_root(db, child)
            wt_path = child.worktree_path or child.root_path
            dir_exists = bool(wt_path) and Path(wt_path).exists()

            if dir_exists:
                cmd = ["git", "-C", parent_root or wt_path, "worktree", "remove"]
                if force:
                    cmd.append("--force")
                cmd.append(wt_path)
                try:
                    proc = subprocess.run(
                        cmd, capture_output=True, text=True, timeout=_GIT_REMOVE_TIMEOUT,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise CodingRuntimeError(400, "git worktree remove timed out") from exc
                except Exception as exc:  # noqa: BLE001
                    raise CodingRuntimeError(400, f"git worktree remove failed: {exc}") from exc
                if proc.returncode != 0:
                    detail = (proc.stderr or proc.stdout or "git worktree remove failed").strip()
                    raise CodingRuntimeError(400, detail)
            else:
                # Dir already gone — prune git's bookkeeping, then drop the row.
                self._prune_worktrees(parent_root)

            db.delete(child)
            db.commit()
            logger.info(
                "Removed worktree space %s (path=%s, dir_existed=%s)",
                worktree_space_id, wt_path, dir_exists,
            )
        finally:
            db.close()

    @staticmethod
    def _prune_worktrees(root_path: str | None) -> None:
        if not _is_git_repo(root_path):
            return
        try:
            subprocess.run(
                ["git", "-C", root_path, "worktree", "prune"],
                capture_output=True, text=True, timeout=_GIT_QUICK_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("git worktree prune failed for %s: %s", root_path, exc)


_SERVICE: CodingWorktreeService | None = None


def get_coding_worktree_service() -> CodingWorktreeService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = CodingWorktreeService()
    return _SERVICE
