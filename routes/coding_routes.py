"""Coding station API routes."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from routes.auth_routes import SESSION_COOKIE

from core.database import (
    CodingModelConfigSnapshot,
    CodingProject,
    CodingRun,
    CodingThread,
    CodingThreadEvent,
    SessionLocal,
)
from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN, require_admin
from src.auth_helpers import require_user
from src.coding_harnesses import get_harness, list_harnesses
from src.coding_model_config import (
    apply_current_model_config,
    derive_odysseus_model_config,
    restore_previous_model_config,
    thread_model_config,
)
from src.coding_runtime import CodingRuntimeError, get_coding_runtime_service
from src.settings import DEFAULT_SETTINGS, load_settings, save_settings


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _owner(request: Request) -> str:
    user = require_user(request)
    if user == "api":
        return (getattr(request.state, "api_token_owner", None) or "").strip()
    return user or ""


def _websocket_client_host(websocket: WebSocket) -> str:
    client = getattr(websocket, "client", None)
    return (client.host if client else "") or ""


def _websocket_internal_owner(websocket: WebSocket) -> str | None:
    try:
        if websocket.headers.get(INTERNAL_TOOL_HEADER) != INTERNAL_TOOL_TOKEN:
            return None
        return (websocket.headers.get("X-Odysseus-Owner") or "").strip() or "internal-tool"
    except Exception:
        return None


def _websocket_owner(websocket: WebSocket) -> str | None:
    internal_owner = _websocket_internal_owner(websocket)
    if internal_owner is not None:
        return internal_owner

    auth_manager = getattr(websocket.app.state, "auth_manager", None)
    token = websocket.cookies.get(SESSION_COOKIE)
    if auth_manager is not None and getattr(auth_manager, "is_configured", False):
        if not auth_manager.validate_token(token):
            return None
        return auth_manager.get_username_for_token(token) or ""

    if _websocket_client_host(websocket) not in ("127.0.0.1", "::1", "localhost"):
        return None
    return ""


def _websocket_admin_allowed(websocket: WebSocket, owner: str) -> bool:
    if _websocket_internal_owner(websocket) is not None:
        return True
    if os.getenv("AUTH_ENABLED", "true").lower() == "false":
        return True
    auth_manager = getattr(websocket.app.state, "auth_manager", None)
    if not auth_manager or not getattr(auth_manager, "is_configured", False):
        return False
    return bool(owner and auth_manager.is_admin(owner))


def _not_found(name: str):
    raise HTTPException(404, f"{name} not found")


def _runtime_error(exc: CodingRuntimeError):
    raise HTTPException(exc.status_code, exc.detail)


def _project_dict(project: CodingProject) -> dict[str, Any]:
    return {
        "id": project.id,
        "owner": project.owner,
        "name": project.name,
        "root_path": project.root_path,
        "description": project.description or "",
        "default_harness": project.default_harness or "generic",
        "default_endpoint_id": project.default_endpoint_id or "",
        "default_model": project.default_model or "",
        "archived": bool(project.archived),
        "created_at": _iso(project.created_at),
        "updated_at": _iso(project.updated_at),
    }


def _thread_dict(thread: CodingThread, model_config: dict[str, Any] | None = None) -> dict[str, Any]:
    item = {
        "id": thread.id,
        "project_id": thread.project_id,
        "owner": thread.owner,
        "session_id": thread.session_id,
        "title": thread.title,
        "cwd": thread.cwd,
        "harness_id": thread.harness_id or "generic",
        "model_endpoint_id": thread.model_endpoint_id or "",
        "model": thread.model or "",
        "pinned_at": _iso(thread.pinned_at),
        "status": thread.status or "idle",
        "last_run_id": thread.last_run_id,
        "metadata": _json_loads(thread.metadata_json, {}),
        "created_at": _iso(thread.created_at),
        "updated_at": _iso(thread.updated_at),
    }
    if model_config is not None:
        item["model_config"] = model_config
    return item


def _run_dict(run: CodingRun) -> dict[str, Any]:
    metadata = _json_loads(run.metadata_json, {})
    return {
        "id": run.id,
        "thread_id": run.thread_id,
        "owner": run.owner,
        "harness_id": run.harness_id,
        "status": run.status,
        "command": run.command,
        "cwd": run.cwd,
        "tmux_session": run.tmux_session,
        "run_dir": run.run_dir,
        "log_path": run.log_path,
        "exit_code": run.exit_code,
        "error": run.error,
        "queued_at": _iso(run.queued_at),
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "idempotency_key": run.idempotency_key,
        "metadata": metadata,
    }


def _event_dict(event: CodingThreadEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "thread_id": event.thread_id,
        "run_id": event.run_id,
        "seq": event.seq,
        "kind": event.kind,
        "payload": _json_loads(event.payload_json, {}),
        "created_at": _iso(event.created_at),
    }


def _snapshot_dict(snapshot: CodingModelConfigSnapshot) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "thread_id": snapshot.thread_id,
        "owner": snapshot.owner,
        "source": snapshot.source,
        "endpoint_id": snapshot.endpoint_id or "",
        "model": snapshot.model or "",
        "payload": _json_loads(snapshot.payload_json, {}),
        "created_at": _iso(snapshot.created_at),
        "restored_at": _iso(snapshot.restored_at),
    }


def _coerce_max_concurrent_agents(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(DEFAULT_SETTINGS.get("coding_max_concurrent_threads", 2) or 2)
    return max(1, min(parsed, 64))


def _coding_settings_dict() -> dict[str, Any]:
    settings = load_settings()
    value = _coerce_max_concurrent_agents(
        settings.get("coding_max_concurrent_threads", DEFAULT_SETTINGS.get("coding_max_concurrent_threads", 2))
    )
    return {
        "max_concurrent_agents": value,
        "max_concurrent_runs": value,
        "coding_max_concurrent_threads": value,
    }


def _project_query(db, owner: str):
    return db.query(CodingProject).filter(CodingProject.owner == owner)


def _thread_query(db, owner: str):
    return db.query(CodingThread).filter(CodingThread.owner == owner)


def _run_query(db, owner: str):
    return db.query(CodingRun).filter(CodingRun.owner == owner)


def _scoped_run_query(db, owner: str, project_id: str | None = None):
    query = _run_query(db, owner).join(CodingThread, CodingThread.id == CodingRun.thread_id)
    query = query.filter(CodingThread.owner == owner)
    if project_id is not None:
        query = query.filter(CodingThread.project_id == project_id)
    return query


def _validate_harness(harness_id: str | None) -> str:
    return get_harness(harness_id or "generic").id


class ProjectCreate(BaseModel):
    name: str
    root_path: str | None = None
    description: str | None = ""
    default_harness: str | None = "generic"
    default_endpoint_id: str | None = ""
    default_model: str | None = ""


class ProjectPatch(BaseModel):
    name: str | None = None
    root_path: str | None = None
    description: str | None = None
    default_harness: str | None = None
    default_endpoint_id: str | None = None
    default_model: str | None = None


class ThreadCreate(BaseModel):
    title: str | None = None
    cwd: str | None = None
    harness_id: str | None = None
    session_id: str | None = None
    model_endpoint_id: str | None = None
    model: str | None = None
    pinned: bool = False
    metadata: dict[str, Any] | None = None


class ThreadPatch(BaseModel):
    title: str | None = None
    cwd: str | None = None
    harness_id: str | None = None
    session_id: str | None = None
    model_endpoint_id: str | None = None
    model: str | None = None
    metadata: dict[str, Any] | None = None
    status: str | None = None


class RunCreate(BaseModel):
    command: str | None = None
    cwd: str | None = None
    harness_id: str | None = None
    model_endpoint_id: str | None = None
    model: str | None = None
    replace: bool = False
    idempotency_key: str | None = None
    metadata: dict[str, Any] | None = None
    cols: int | None = None
    rows: int | None = None


class StdinRequest(BaseModel):
    data: str


class ResizeRequest(BaseModel):
    cols: int
    rows: int


class CodingSettingsPatch(BaseModel):
    max_concurrent_agents: int | None = None
    max_concurrent_runs: int | None = None
    coding_max_concurrent_threads: int | None = None


def setup_coding_routes() -> APIRouter:
    router = APIRouter(prefix="/api/coding", tags=["coding"])
    runtime = get_coding_runtime_service()

    @router.get("/projects")
    async def list_projects(request: Request, include_archived: bool = Query(False)):
        owner = _owner(request)
        db = SessionLocal()
        try:
            query = _project_query(db, owner)
            if not include_archived:
                query = query.filter(CodingProject.archived == False)  # noqa: E712
            projects = query.order_by(CodingProject.updated_at.desc()).all()
            return {"projects": [_project_dict(project) for project in projects]}
        finally:
            db.close()

    @router.post("/projects")
    async def create_project(request: Request, body: ProjectCreate):
        owner = _owner(request)
        name = (body.name or "").strip()
        if not name:
            raise HTTPException(400, "Project name is required")
        try:
            harness_id = _validate_harness(body.default_harness)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        root_path = str(Path(body.root_path or os.getcwd()).expanduser())
        db = SessionLocal()
        try:
            project = CodingProject(
                id=str(uuid.uuid4()),
                owner=owner,
                name=name,
                root_path=root_path,
                description=body.description or "",
                default_harness=harness_id,
                default_endpoint_id=(body.default_endpoint_id or "").strip(),
                default_model=(body.default_model or "").strip(),
                archived=False,
            )
            db.add(project)
            db.commit()
            db.refresh(project)
            return {"project": _project_dict(project)}
        finally:
            db.close()

    @router.get("/projects/{project_id}")
    async def get_project(request: Request, project_id: str):
        owner = _owner(request)
        db = SessionLocal()
        try:
            project = _project_query(db, owner).filter(CodingProject.id == project_id).first()
            if not project:
                _not_found("Project")
            return {"project": _project_dict(project)}
        finally:
            db.close()

    @router.patch("/projects/{project_id}")
    async def patch_project(request: Request, project_id: str, body: ProjectPatch):
        owner = _owner(request)
        db = SessionLocal()
        try:
            project = _project_query(db, owner).filter(CodingProject.id == project_id).first()
            if not project:
                _not_found("Project")
            if body.name is not None:
                name = body.name.strip()
                if not name:
                    raise HTTPException(400, "Project name is required")
                project.name = name
            if body.root_path is not None:
                project.root_path = str(Path(body.root_path).expanduser())
            if body.description is not None:
                project.description = body.description
            if body.default_harness is not None:
                try:
                    project.default_harness = _validate_harness(body.default_harness)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
            if body.default_endpoint_id is not None:
                project.default_endpoint_id = body.default_endpoint_id.strip()
            if body.default_model is not None:
                project.default_model = body.default_model.strip()
            project.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(project)
            return {"project": _project_dict(project)}
        finally:
            db.close()

    @router.post("/projects/{project_id}/archive")
    async def archive_project(request: Request, project_id: str):
        owner = _owner(request)
        db = SessionLocal()
        try:
            project = _project_query(db, owner).filter(CodingProject.id == project_id).first()
            if not project:
                _not_found("Project")
            project.archived = True
            project.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(project)
            return {"project": _project_dict(project)}
        finally:
            db.close()

    @router.post("/projects/{project_id}/restore")
    async def restore_project(request: Request, project_id: str):
        owner = _owner(request)
        db = SessionLocal()
        try:
            project = _project_query(db, owner).filter(CodingProject.id == project_id).first()
            if not project:
                _not_found("Project")
            project.archived = False
            project.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(project)
            return {"project": _project_dict(project)}
        finally:
            db.close()

    @router.get("/projects/{project_id}/threads")
    async def list_threads(request: Request, project_id: str):
        owner = _owner(request)
        db = SessionLocal()
        try:
            project = _project_query(db, owner).filter(CodingProject.id == project_id).first()
            if not project:
                _not_found("Project")
            threads = (
                _thread_query(db, owner)
                .filter(CodingThread.project_id == project_id)
                .order_by(CodingThread.pinned_at.is_(None), CodingThread.pinned_at.desc(), CodingThread.updated_at.desc())
                .all()
            )
            return {"threads": [_thread_dict(thread, thread_model_config(db, thread)) for thread in threads]}
        finally:
            db.close()

    @router.post("/projects/{project_id}/threads")
    async def create_thread(request: Request, project_id: str, body: ThreadCreate):
        owner = _owner(request)
        db = SessionLocal()
        try:
            project = _project_query(db, owner).filter(CodingProject.id == project_id).first()
            if not project:
                _not_found("Project")
            if project.archived:
                raise HTTPException(409, "Project is archived")
            try:
                harness_id = _validate_harness(body.harness_id or project.default_harness)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            default_config = derive_odysseus_model_config(db, owner)
            endpoint_id = (
                body.model_endpoint_id
                if body.model_endpoint_id is not None
                else (project.default_endpoint_id or default_config.get("endpoint_id") or "")
            )
            model = (
                body.model
                if body.model is not None
                else (project.default_model or default_config.get("model") or "")
            )
            thread = CodingThread(
                id=str(uuid.uuid4()),
                project_id=project.id,
                owner=owner,
                session_id=(body.session_id or "").strip() or None,
                title=(body.title or project.name or "Coding Thread").strip(),
                cwd=str(Path(body.cwd or project.root_path).expanduser()),
                harness_id=harness_id,
                model_endpoint_id=(endpoint_id or "").strip(),
                model=(model or "").strip(),
                pinned_at=datetime.utcnow() if body.pinned else None,
                status="idle",
                metadata_json=json.dumps(body.metadata or {}),
            )
            db.add(thread)
            project.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(thread)
            return {"thread": _thread_dict(thread, thread_model_config(db, thread))}
        finally:
            db.close()

    @router.get("/threads/{thread_id}")
    async def get_thread(request: Request, thread_id: str):
        owner = _owner(request)
        db = SessionLocal()
        try:
            thread = _thread_query(db, owner).filter(CodingThread.id == thread_id).first()
            if not thread:
                _not_found("Thread")
            last_run = None
            if thread.last_run_id:
                last_run = _run_query(db, owner).filter(CodingRun.id == thread.last_run_id).first()
            payload = {"thread": _thread_dict(thread, thread_model_config(db, thread))}
            if last_run:
                payload["run"] = _run_dict(last_run)
            return payload
        finally:
            db.close()

    @router.patch("/threads/{thread_id}")
    async def patch_thread(request: Request, thread_id: str, body: ThreadPatch):
        owner = _owner(request)
        db = SessionLocal()
        try:
            thread = _thread_query(db, owner).filter(CodingThread.id == thread_id).first()
            if not thread:
                _not_found("Thread")
            if body.title is not None:
                title = body.title.strip()
                if not title:
                    raise HTTPException(400, "Thread title is required")
                thread.title = title
            if body.cwd is not None:
                thread.cwd = str(Path(body.cwd).expanduser())
            if body.harness_id is not None:
                try:
                    thread.harness_id = _validate_harness(body.harness_id)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
            if body.session_id is not None:
                thread.session_id = body.session_id.strip() or None
            if body.model_endpoint_id is not None:
                thread.model_endpoint_id = body.model_endpoint_id.strip()
            if body.model is not None:
                thread.model = body.model.strip()
            if body.metadata is not None:
                thread.metadata_json = json.dumps(body.metadata)
            if body.status is not None:
                if body.status not in {"idle", "queued", "starting", "running", "stopping"}:
                    raise HTTPException(400, "Invalid thread status")
                thread.status = body.status
            thread.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(thread)
            return {"thread": _thread_dict(thread, thread_model_config(db, thread))}
        finally:
            db.close()

    @router.post("/threads/{thread_id}/pin")
    async def pin_thread(request: Request, thread_id: str):
        owner = _owner(request)
        db = SessionLocal()
        try:
            thread = _thread_query(db, owner).filter(CodingThread.id == thread_id).first()
            if not thread:
                _not_found("Thread")
            thread.pinned_at = datetime.utcnow()
            thread.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(thread)
            return {"thread": _thread_dict(thread, thread_model_config(db, thread))}
        finally:
            db.close()

    @router.delete("/threads/{thread_id}/pin")
    async def unpin_thread(request: Request, thread_id: str):
        owner = _owner(request)
        db = SessionLocal()
        try:
            thread = _thread_query(db, owner).filter(CodingThread.id == thread_id).first()
            if not thread:
                _not_found("Thread")
            thread.pinned_at = None
            thread.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(thread)
            return {"thread": _thread_dict(thread, thread_model_config(db, thread))}
        finally:
            db.close()

    @router.delete("/threads/{thread_id}")
    async def delete_thread(request: Request, thread_id: str):
        require_admin(request)
        owner = _owner(request)
        try:
            await runtime.delete_thread(thread_id, owner)
            return {"ok": True, "deleted": thread_id}
        except CodingRuntimeError as exc:
            _runtime_error(exc)

    @router.post("/threads/{thread_id}/run")
    async def run_thread(request: Request, thread_id: str, body: RunCreate):
        require_admin(request)
        owner = _owner(request)
        try:
            run = await runtime.enqueue_run(
                thread_id=thread_id,
                owner=owner,
                command=body.command,
                cwd=body.cwd,
                harness_id=body.harness_id,
                model_endpoint_id=body.model_endpoint_id,
                model=body.model,
                replace=body.replace,
                idempotency_key=body.idempotency_key,
                metadata=body.metadata,
                cols=body.cols,
                rows=body.rows,
            )
            return {"run": _run_dict(run)}
        except CodingRuntimeError as exc:
            _runtime_error(exc)

    @router.get("/threads/{thread_id}/events")
    async def get_thread_events(request: Request, thread_id: str, after_seq: int = Query(0)):
        owner = _owner(request)
        try:
            events = runtime.get_events(thread_id, owner, after_seq=max(0, after_seq))
            return {"events": [_event_dict(event) for event in events]}
        except CodingRuntimeError as exc:
            _runtime_error(exc)

    @router.get("/threads/{thread_id}/stream")
    async def stream_thread(request: Request, thread_id: str, after_seq: int = Query(0)):
        owner = _owner(request)
        try:
            runtime.get_events(thread_id, owner, after_seq=max(0, after_seq))
        except CodingRuntimeError as exc:
            _runtime_error(exc)

        async def generate():
            async for item in runtime.event_stream(
                thread_id=thread_id,
                owner=owner,
                after_seq=max(0, after_seq),
                request=request,
            ):
                yield f"event: {item['kind']}\ndata: {json.dumps(item)}\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")

    @router.get("/runs/{run_id}")
    async def get_run(request: Request, run_id: str):
        owner = _owner(request)
        db = SessionLocal()
        try:
            run = _run_query(db, owner).filter(CodingRun.id == run_id).first()
            if not run:
                _not_found("Run")
            return {"run": _run_dict(run)}
        finally:
            db.close()

    @router.get("/runs/{run_id}/stream")
    async def stream_run(request: Request, run_id: str, after_seq: int = Query(0)):
        owner = _owner(request)
        db = SessionLocal()
        try:
            run = _run_query(db, owner).filter(CodingRun.id == run_id).first()
            if not run:
                _not_found("Run")
            thread_id = run.thread_id
        finally:
            db.close()

        async def generate():
            async for item in runtime.event_stream(
                thread_id=thread_id,
                owner=owner,
                after_seq=max(0, after_seq),
                request=request,
                run_id=run_id,
            ):
                yield f"event: {item['kind']}\ndata: {json.dumps(item)}\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")

    @router.websocket("/runs/{run_id}/pty")
    async def run_pty(websocket: WebSocket, run_id: str):
        # WebSocket auth: BaseHTTPMiddleware does not run for websockets, so validate the
        # session cookie here (mirrors the HTTP AuthMiddleware + require_user fallback).
        owner = _websocket_owner(websocket)
        if owner is None or not _websocket_admin_allowed(websocket, owner):
            await websocket.close(code=1008)
            return

        def _int_param(name: str, default: int) -> int:
            try:
                return int(websocket.query_params.get(name, default))
            except (TypeError, ValueError):
                return default

        cols = _int_param("cols", 120)
        rows = _int_param("rows", 40)
        await websocket.accept()
        try:
            await runtime.attach_pty(websocket, run_id, owner, cols, rows)
        except CodingRuntimeError as exc:
            try:
                await websocket.send_json({"type": "error", "detail": exc.detail})
            except Exception:
                pass
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            try:
                await websocket.close()
            except Exception:
                pass

    @router.post("/runs/{run_id}/stdin")
    async def send_stdin(request: Request, run_id: str, body: StdinRequest):
        require_admin(request)
        owner = _owner(request)
        try:
            run = await runtime.send_stdin(run_id, owner, body.data)
            return {"run": _run_dict(run)}
        except CodingRuntimeError as exc:
            _runtime_error(exc)

    @router.post("/runs/{run_id}/resize")
    async def resize_run(request: Request, run_id: str, body: ResizeRequest):
        require_admin(request)
        owner = _owner(request)
        try:
            run = await runtime.resize(run_id, owner, body.cols, body.rows)
            return {"run": _run_dict(run)}
        except CodingRuntimeError as exc:
            _runtime_error(exc)

    @router.post("/runs/{run_id}/stop")
    async def stop_run(request: Request, run_id: str):
        require_admin(request)
        owner = _owner(request)
        try:
            run = await runtime.stop_run(run_id, owner)
            return {"run": _run_dict(run)}
        except CodingRuntimeError as exc:
            _runtime_error(exc)

    @router.get("/queue")
    async def get_queue(request: Request, project_id: str | None = Query(None)):
        owner = _owner(request)
        project_scope = (project_id or "").strip() or None
        db = SessionLocal()
        try:
            if project_scope:
                project = _project_query(db, owner).filter(CodingProject.id == project_scope).first()
                if not project:
                    _not_found("Project")
        finally:
            db.close()

        # Concurrency is a single owner-wide pool shared across projects; pump globally.
        await runtime.pump_queue(owner=owner)
        db = SessionLocal()
        try:
            # Owner-global rows joined with thread + project so the UI can attribute
            # each run (and the pool totals) to the project that is consuming it.
            rows = (
                db.query(CodingRun, CodingThread, CodingProject)
                .join(CodingThread, CodingThread.id == CodingRun.thread_id)
                .join(CodingProject, CodingProject.id == CodingThread.project_id)
                .filter(CodingRun.owner == owner, CodingThread.owner == owner)
                .filter(CodingRun.status.in_(("queued", "starting", "running", "stopping")))
                .order_by(CodingRun.queued_at.asc())
                .all()
            )
            active_runs: list[dict[str, Any]] = []
            queued_runs: list[dict[str, Any]] = []
            by_project: dict[str, dict[str, Any]] = {}
            for run, thread, project in rows:
                bucket = by_project.setdefault(
                    project.id,
                    {"project_id": project.id, "project_name": project.name, "active": 0, "queued": 0},
                )
                entry = _run_dict(run)
                entry["project_id"] = project.id
                entry["project_name"] = project.name
                entry["thread_title"] = thread.title
                if run.status == "queued":
                    queued_runs.append(entry)
                    bucket["queued"] += 1
                else:
                    active_runs.append(entry)
                    bucket["active"] += 1
            # Totals are owner-global; project_scope is only echoed back so the client
            # knows which project it asked about (it does not narrow the counts).
            queue = runtime.queue_snapshot(owner, project_id=project_scope)
            queue.update(
                {
                    "queued_runs": queued_runs,
                    "active_runs": active_runs,
                    "by_project": sorted(
                        by_project.values(), key=lambda b: (b["project_name"] or "").lower()
                    ),
                }
            )
            return {"queue": queue}
        finally:
            db.close()

    @router.get("/settings")
    async def get_coding_settings(request: Request):
        _owner(request)
        return {"settings": _coding_settings_dict()}

    @router.patch("/settings")
    async def patch_coding_settings(request: Request, body: CodingSettingsPatch):
        require_admin(request)
        _owner(request)
        raw_value = (
            body.max_concurrent_agents
            if body.max_concurrent_agents is not None
            else body.max_concurrent_runs
            if body.max_concurrent_runs is not None
            else body.coding_max_concurrent_threads
        )
        if raw_value is None:
            raise HTTPException(400, "max_concurrent_agents is required")
        settings = load_settings()
        settings["coding_max_concurrent_threads"] = _coerce_max_concurrent_agents(raw_value)
        save_settings(settings)
        return {"settings": _coding_settings_dict()}

    @router.post("/settings")
    async def post_coding_settings(request: Request, body: CodingSettingsPatch):
        return await patch_coding_settings(request, body)

    @router.get("/harnesses")
    async def get_harnesses(request: Request):
        _owner(request)
        return {"harnesses": list_harnesses()}

    @router.get("/model-config")
    async def get_model_config(request: Request):
        owner = _owner(request)
        db = SessionLocal()
        try:
            return {"model_config": derive_odysseus_model_config(db, owner)}
        finally:
            db.close()

    @router.post("/threads/{thread_id}/derive-model-config")
    async def derive_thread_model_config(request: Request, thread_id: str):
        owner = _owner(request)
        db = SessionLocal()
        try:
            thread = _thread_query(db, owner).filter(CodingThread.id == thread_id).first()
            if not thread:
                _not_found("Thread")
            model_config, snapshot = apply_current_model_config(db, thread, owner)
            db.commit()
            db.refresh(thread)
            db.refresh(snapshot)
            await runtime.append_event(
                thread.id,
                None,
                "model_config_derived",
                {"model_config": model_config, "snapshot_id": snapshot.id},
            )
            return {
                "thread": _thread_dict(thread, thread_model_config(db, thread)),
                "model_config": model_config,
                "snapshot": _snapshot_dict(snapshot),
            }
        finally:
            db.close()

    @router.post("/threads/{thread_id}/restore-config")
    async def restore_thread_model_config(request: Request, thread_id: str):
        owner = _owner(request)
        db = SessionLocal()
        try:
            thread = _thread_query(db, owner).filter(CodingThread.id == thread_id).first()
            if not thread:
                _not_found("Thread")
            restored = restore_previous_model_config(db, thread, owner)
            if restored is None:
                raise HTTPException(404, "No unrestored model config snapshot")
            model_config, snapshot = restored
            db.commit()
            db.refresh(thread)
            db.refresh(snapshot)
            await runtime.append_event(
                thread.id,
                None,
                "model_config_restored",
                {"model_config": model_config, "snapshot_id": snapshot.id},
            )
            return {
                "thread": _thread_dict(thread, thread_model_config(db, thread)),
                "model_config": model_config,
                "snapshot": _snapshot_dict(snapshot),
            }
        finally:
            db.close()

    return router
