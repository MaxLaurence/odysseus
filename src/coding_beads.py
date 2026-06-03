"""Beads (`bd`) integration — repo-scoped, dependency-aware work tracking.

Beads is a git-native issue tracker built for AI coding agents: issues live in a
``.beads/`` directory inside the repo (a Dolt store in ``bd`` 1.x), travel with
the code through git, carry an explicit dependency graph, and expose an offline
``bd ready`` queue of unblocked work. In the Odysseus "who owns what" split this
is the **source of truth for work**:

    Memory   = durable facts about the *user* (forgettable, cross-project)
    RAG/Docs = reference knowledge (read-mostly)
    Beads    = work (structured, repo-scoped, durable)  ← this module

This service is the single seam every caller goes through — the REST routes, the
``beads`` provider tool, and the ``ody bead`` socket resource all call it, exactly
the way they already share ``CodingRuntimeService`` / ``CodingWorkspaceService``.

Design rules:
  * **Read through the ``bd`` CLI, never the raw ``.beads/`` files.** ``bd`` owns
    its store (Dolt cache + optional JSONL export); parsing those directly would
    drift. Every method shells out to ``bd … --json``.
  * Beads is **repo-scoped, not owner-scoped.** Callers resolve a ``root_path``
    from the owner-scoped ``CodingProject`` first, so the owner boundary is
    enforced *before* we ever reach this layer.
  * ``bd init`` is invasive by default (writes AGENTS.md / CLAUDE.md, installs git
    hooks). :meth:`init` runs the minimal, hygienic form
    (``--skip-agents --skip-hooks --non-interactive``) so enabling Beads on a
    project does not clobber the repo owner's files.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


class BeadsError(Exception):
    """Mirrors CodingRuntimeError/CodingProviderError so callers map status codes
    consistently (404 missing issue, 409 not initialised, 503 ``bd`` absent…)."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# Valid issue statuses (mirrors `bd statuses`); kept here so callers can validate
# without shelling out. Beads is the authority — this is a convenience copy.
ISSUE_STATUSES = ("open", "in_progress", "blocked", "closed", "deferred")
# Edge in the `bd graph --dot` output: "<blocker>" -> "<blocked>".
_DOT_EDGE_RE = re.compile(r'"([^"]+)"\s*->\s*"([^"]+)"')

# Common locations the `bd` binary lands in (Homebrew, pipx/local, system) so the
# packaged .app — which can launch with a minimal PATH — still finds it.
_BD_FALLBACK_PATHS = (
    "/opt/homebrew/bin/bd",
    "/usr/local/bin/bd",
    "/usr/bin/bd",
)

_DEFAULT_TIMEOUT = 45.0
_INIT_TIMEOUT = 150.0  # first run boots the embedded Dolt engine; be generous.


def bd_bin() -> str | None:
    """Absolute path to the ``bd`` binary, or ``None`` if not installed.

    Resolution order: ``$ODYSSEUS_BD_BIN`` / ``$BEADS_BIN`` override → ``PATH``
    (``shutil.which``) → well-known install dirs. The override lets the packaged
    app point at a bundled binary.
    """
    for key in ("ODYSSEUS_BD_BIN", "BEADS_BIN"):
        explicit = (os.environ.get(key) or "").strip()
        if explicit and os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return explicit
    found = shutil.which("bd")
    if found:
        return found
    for candidate in _BD_FALLBACK_PATHS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def bd_dir() -> str | None:
    """Directory containing ``bd`` (for prepending to a launched run's PATH)."""
    binary = bd_bin()
    return str(Path(binary).resolve().parent) if binary else None


class BeadsService:
    """Stateless wrapper over the ``bd`` CLI, scoped per ``root_path``."""

    def available(self) -> bool:
        return bd_bin() is not None

    def is_initialized(self, root_path: str | None) -> bool:
        if not root_path:
            return False
        try:
            return (Path(root_path).expanduser() / ".beads").is_dir()
        except OSError:
            return False

    # ------------------------------------------------------------------ run
    def _run(
        self,
        root_path: str | None,
        args: list[str],
        *,
        json_output: bool = True,
        timeout: float = _DEFAULT_TIMEOUT,
        require_init: bool = True,
    ) -> Any:
        binary = bd_bin()
        if not binary:
            raise BeadsError(
                503,
                "bd (Beads) is not installed. Install it (e.g. `brew install beads`) "
                "or set ODYSSEUS_BD_BIN to its path.",
            )
        root = str(Path(root_path or os.getcwd()).expanduser())
        if not os.path.isdir(root):
            raise BeadsError(400, f"Project root does not exist: {root}")
        if require_init and not self.is_initialized(root):
            raise BeadsError(409, "Beads is not enabled for this project (run init first).")

        # Locate the project via cwd (bd discovers `.beads/` from there upward).
        # NOT via the global `-C` flag: `-C` requires an *existing* project, so it
        # breaks `bd init`; cwd works uniformly for init and every other command.
        cmd = [binary, *args]
        if json_output and "--json" not in args:
            cmd.append("--json")

        env = dict(os.environ)
        env["BD_NON_INTERACTIVE"] = "1"  # never block on a prompt under subprocess
        env.setdefault("NO_COLOR", "1")
        try:
            proc = subprocess.run(
                cmd,
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise BeadsError(504, f"bd timed out after {timeout:.0f}s") from exc
        except OSError as exc:
            raise BeadsError(500, f"failed to run bd: {exc}") from exc

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip() or f"bd exited {proc.returncode}"
            # bd prints multi-line errors; keep it to the first meaningful line.
            detail = detail.splitlines()[0][:500]
            raise BeadsError(400, f"bd: {detail}")

        if not json_output:
            return (proc.stdout or "").strip()
        out = (proc.stdout or "").strip()
        if not out:
            return []
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise BeadsError(502, f"could not parse bd JSON output: {exc}") from exc

    # --------------------------------------------------------------- lifecycle
    def init(self, root_path: str, *, prefix: str | None = None) -> dict[str, Any]:
        """Enable Beads for a repo with the *minimal* footprint.

        ``--skip-agents`` (no AGENTS.md / CLAUDE.md rewrite) and ``--skip-hooks``
        (no git-hook install) keep us from clobbering the repo owner's files; the
        agent already learns the Beads workflow from the injected ody guide.
        Idempotent: a repo that already has ``.beads/`` is reported as such.
        """
        root = str(Path(root_path or "").expanduser())
        if not os.path.isdir(root):
            raise BeadsError(400, f"Project root does not exist: {root}")
        if self.is_initialized(root):
            return {"initialized": True, "already_initialized": True, "root_path": root}
        args = ["init", "--non-interactive", "--quiet", "--skip-agents", "--skip-hooks"]
        if prefix and prefix.strip():
            args.extend(["--prefix", prefix.strip()])
        self._run(root, args, json_output=False, timeout=_INIT_TIMEOUT, require_init=False)
        if not self.is_initialized(root):
            raise BeadsError(500, "bd init completed but .beads/ was not created")
        return {"initialized": True, "already_initialized": False, "root_path": root}

    # -------------------------------------------------------------------- reads
    def list_issues(
        self,
        root_path: str,
        *,
        include_closed: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        args = ["list", "-n", str(max(1, min(int(limit or 200), 1000)))]
        if include_closed:
            args.append("--all")
        rows = self._run(root_path, args)
        return [self._issue_dict(r) for r in rows if isinstance(r, dict)]

    def ready(self, root_path: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._run(root_path, ["ready", "-n", str(max(1, min(int(limit or 100), 1000)))])
        return [self._issue_dict(r) for r in rows if isinstance(r, dict)]

    def show(self, root_path: str, issue_id: str) -> dict[str, Any]:
        issue_id = (issue_id or "").strip()
        if not issue_id:
            raise BeadsError(400, "issue id is required")
        rows = self._run(root_path, ["show", issue_id])
        if isinstance(rows, list):
            rows = [r for r in rows if isinstance(r, dict)]
            if not rows:
                raise BeadsError(404, f"Issue not found: {issue_id}")
            return self._issue_dict(rows[0], include_dependencies=True)
        if isinstance(rows, dict):
            return self._issue_dict(rows, include_dependencies=True)
        raise BeadsError(404, f"Issue not found: {issue_id}")

    def summary(self, root_path: str) -> dict[str, int]:
        """Counts for the per-project rollup, from a single ``bd status --json``."""
        data = self._run(root_path, ["status"])
        summary = (data or {}).get("summary", {}) if isinstance(data, dict) else {}
        return {
            "open": int(summary.get("open_issues", 0) or 0),
            "in_progress": int(summary.get("in_progress_issues", 0) or 0),
            "blocked": int(summary.get("blocked_issues", 0) or 0),
            "closed": int(summary.get("closed_issues", 0) or 0),
            "deferred": int(summary.get("deferred_issues", 0) or 0),
            "ready": int(summary.get("ready_issues", 0) or 0),
            "total": int(summary.get("total_issues", 0) or 0),
        }

    def graph(self, root_path: str) -> dict[str, Any]:
        """Dependency DAG for the whole project: nodes (every open issue) + edges
        (``blocker -> blocked``). Nodes come from ``bd list``; edges are parsed
        from ``bd graph --dot --all`` (one ``bd`` call for the entire graph)."""
        nodes = self.list_issues(root_path, include_closed=False, limit=1000)
        node_ids = {n["id"] for n in nodes}
        dot = self._run(root_path, ["graph", "--dot", "--all"], json_output=False)
        edges: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for blocker, blocked in _DOT_EDGE_RE.findall(dot or ""):
            key = (blocker, blocked)
            if key in seen:
                continue
            seen.add(key)
            # Only surface edges whose endpoints are both live nodes we returned.
            if blocker in node_ids and blocked in node_ids:
                edges.append({"from": blocker, "to": blocked})
        return {"nodes": nodes, "edges": edges}

    # -------------------------------------------------------------------- writes
    def create(
        self,
        root_path: str,
        title: str,
        *,
        issue_type: str | None = None,
        priority: int | None = None,
        description: str | None = None,
        discovered_from: str | None = None,
    ) -> dict[str, Any]:
        title = (title or "").strip()
        if not title:
            raise BeadsError(400, "title is required")
        args = ["create", title]
        if issue_type and issue_type.strip():
            args.extend(["-t", issue_type.strip()])
        if priority is not None:
            args.extend(["-p", str(int(priority))])
        if description and description.strip():
            args.extend(["-d", description.strip()])
        created = self._run(root_path, args)
        issue = created[0] if isinstance(created, list) and created else created
        if not isinstance(issue, dict):
            raise BeadsError(502, "bd create did not return an issue")
        issue = self._issue_dict(issue)
        # `discovered-from` captures "found while working on X" — the provenance
        # link that's Beads' answer to work lost across context compaction.
        if discovered_from and discovered_from.strip() and issue.get("id"):
            try:
                self.dep_add(root_path, issue["id"], discovered_from.strip(), dep_type="discovered-from")
            except BeadsError:
                pass  # the issue exists; a failed provenance link must not lose it
        return issue

    def update(
        self,
        root_path: str,
        issue_id: str,
        *,
        status: str | None = None,
        priority: int | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        issue_id = (issue_id or "").strip()
        if not issue_id:
            raise BeadsError(400, "issue id is required")
        args = ["update", issue_id]
        if status and status.strip():
            if status.strip() not in ISSUE_STATUSES:
                raise BeadsError(400, f"invalid status: {status}")
            args.extend(["--status", status.strip()])
        if priority is not None:
            args.extend(["-p", str(int(priority))])
        if title and title.strip():
            args.extend(["--title", title.strip()])
        if len(args) == 2:
            raise BeadsError(400, "nothing to update")
        self._run(root_path, args)
        return self.show(root_path, issue_id)

    def close(self, root_path: str, issue_ids: list[str], *, reason: str | None = None) -> dict[str, Any]:
        ids = [i.strip() for i in (issue_ids or []) if i and i.strip()]
        if not ids:
            raise BeadsError(400, "at least one issue id is required")
        args = ["close", *ids]
        if reason and reason.strip():
            args.extend(["--reason", reason.strip()])
        self._run(root_path, args, json_output=False)
        return {"closed": ids}

    def dep_add(
        self,
        root_path: str,
        blocked_id: str,
        blocker_id: str,
        *,
        dep_type: str = "blocks",
    ) -> dict[str, Any]:
        """``blocked_id`` depends on ``blocker_id`` (``bd dep add <blocked> <blocker>``)."""
        blocked_id = (blocked_id or "").strip()
        blocker_id = (blocker_id or "").strip()
        if not blocked_id or not blocker_id:
            raise BeadsError(400, "both blocked and blocker issue ids are required")
        args = ["dep", "add", blocked_id, blocker_id]
        if dep_type and dep_type.strip() and dep_type.strip() != "blocks":
            args.extend(["--type", dep_type.strip()])
        self._run(root_path, args, json_output=False)
        return {"blocked": blocked_id, "blocker": blocker_id, "type": dep_type}

    def dep_remove(self, root_path: str, blocked_id: str, blocker_id: str) -> dict[str, Any]:
        """Drop the ``blocked_id`` → ``blocker_id`` dependency (``bd dep remove``)."""
        blocked_id = (blocked_id or "").strip()
        blocker_id = (blocker_id or "").strip()
        if not blocked_id or not blocker_id:
            raise BeadsError(400, "both blocked and blocker issue ids are required")
        self._run(root_path, ["dep", "remove", blocked_id, blocker_id], json_output=False)
        return {"blocked": blocked_id, "blocker": blocker_id, "removed": True}

    # -------------------------------------------------------------------- shaping
    @staticmethod
    def _issue_dict(raw: dict[str, Any], *, include_dependencies: bool = False) -> dict[str, Any]:
        """Normalise a bd issue row to a stable, secret-free domain shape.

        This is the contract the API / UI / provider tool all consume: an explicit
        allowlist of issue fields. It carries no paths, no secrets, and no
        ``bd``-internal bookkeeping, so it survives the provider-tool result
        sanitizer unchanged without depending on that sanitizer's blocklist.
        """
        out: dict[str, Any] = {
            "id": raw.get("id"),
            "title": raw.get("title", ""),
            "status": raw.get("status", "open"),
            "priority": raw.get("priority"),
            "issue_type": raw.get("issue_type", ""),
            "created_at": raw.get("created_at"),
            "updated_at": raw.get("updated_at"),
            "dependency_count": raw.get("dependency_count", 0),
            "dependent_count": raw.get("dependent_count", 0),
            "comment_count": raw.get("comment_count", 0),
        }
        if include_dependencies and isinstance(raw.get("dependencies"), list):
            out["dependencies"] = [
                {
                    "id": d.get("id"),
                    "title": d.get("title", ""),
                    "status": d.get("status", "open"),
                    "dependency_type": d.get("dependency_type", "blocks"),
                }
                for d in raw["dependencies"]
                if isinstance(d, dict)
            ]
        return out


_SERVICE: BeadsService | None = None


def get_beads_service() -> BeadsService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = BeadsService()
    return _SERVICE


__all__ = [
    "BeadsError",
    "BeadsService",
    "ISSUE_STATUSES",
    "bd_bin",
    "bd_dir",
    "get_beads_service",
]
