"""Process-lifecycle safety nets for the desktop backend.

The macOS SwiftUI wrapper (``BackendController``) launches this backend as a
child process. macOS has no ``PR_SET_PDEATHSIG``, so if the wrapper crashes, is
force-quit, or exits without calling ``terminate()``, nothing tells the backend
to stop — it (and its MCP-server children) become orphans reparented to
``launchd`` and keep holding ports/sockets. These helpers make the backend
self-terminate when its launcher dies (the watchdog) and best-effort reap any
subprocesses we would otherwise leave behind (the reaper).

The launcher cooperates by exporting ``ODYSSEUS_PARENT_PID`` in the backend's
environment; when that var is absent (plain ``uvicorn`` in a dev shell, tests)
the watchdog simply no-ops.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import signal
import subprocess
import time

logger = logging.getLogger(__name__)

PARENT_PID_ENV = "ODYSSEUS_PARENT_PID"
_WATCHDOG_POLL_SECONDS = 3.0
_REAP_GRACE_SECONDS = 2.0

_exit_reaper_installed = False
_parent_death_watchdog_disarmed = False


def disarm_parent_death_watchdog(reason: str = "") -> None:
    """Let an explicit desktop "Leave Running" quit keep the backend alive."""
    global _parent_death_watchdog_disarmed
    _parent_death_watchdog_disarmed = True
    logger.info("Parent-death watchdog disarmed%s", f": {reason}" if reason else "")


def _pid_alive(pid: int) -> bool:
    """True if ``pid`` exists. Signal 0 performs only the existence/permission
    check without delivering anything."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — alive enough for our purposes.
        return True
    return True


async def parent_death_watchdog() -> None:
    """Terminate this backend when its launcher process disappears (fix B).

    Arms only when ``ODYSSEUS_PARENT_PID`` is set (i.e. launched by the desktop
    wrapper). On the launcher's disappearance it raises ``SIGTERM`` on *this*
    process so uvicorn runs its normal graceful shutdown — which tears down the
    MCP servers and coding runtime through the FastAPI lifespan handler — rather
    than leaving everything orphaned.
    """
    raw = os.environ.get(PARENT_PID_ENV)
    if not raw:
        return
    try:
        parent_pid = int(raw)
    except (TypeError, ValueError):
        return
    if parent_pid <= 1:
        return

    # Prefer reparenting detection (getppid() flips to 1 the instant our direct
    # parent dies). If we were not launched as a direct child (e.g. via an
    # intermediate shell), fall back to polling the pid for liveness so we don't
    # fire a false positive on the very first tick.
    direct_child = os.getppid() == parent_pid
    logger.info(
        "Parent-death watchdog armed (launcher pid=%s, direct_child=%s)",
        parent_pid,
        direct_child,
    )
    try:
        while True:
            await asyncio.sleep(_WATCHDOG_POLL_SECONDS)
            if _parent_death_watchdog_disarmed:
                logger.info("Parent-death watchdog exiting because it was disarmed")
                return
            if direct_child:
                orphaned = os.getppid() != parent_pid
            else:
                orphaned = not _pid_alive(parent_pid)
            if not orphaned:
                continue
            logger.warning(
                "Launcher pid=%s is gone — shutting backend down to avoid "
                "orphaning it and its MCP children",
                parent_pid,
            )
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            except Exception as e:  # pragma: no cover - extremely unlikely
                logger.error("Watchdog failed to self-signal: %s", e)
            return
    except asyncio.CancelledError:
        raise


def _direct_child_pids() -> list[int]:
    """PIDs whose parent is us. Detached coding sessions (dtach/tmux) deliberately
    reparent away, so they are *not* included — only tightly-coupled helpers like
    the stdio MCP-server subprocesses show up here."""
    try:
        completed = subprocess.run(
            ["pgrep", "-P", str(os.getpid())],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for token in completed.stdout.split():
        try:
            pids.append(int(token))
        except ValueError:
            continue
    return pids


def reap_child_processes(grace_seconds: float = _REAP_GRACE_SECONDS) -> int:
    """Best-effort SIGTERM→SIGKILL of remaining direct child processes (fix C).

    The clean shutdown path already disconnects MCP servers; this is the safety
    net for anything (an MCP subprocess, provider tool, helper) still alive when
    we exit, so it can't outlive the backend. Returns how many children were
    signalled. Safe to call from ``atexit`` (no event loop required) and a no-op
    when there are no children.
    """
    children = _direct_child_pids()
    if not children:
        return 0
    for pid in children:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    # Poll (not busy-spin) for the grace window, then SIGKILL survivors.
    waited = 0.0
    step = 0.1
    while waited < grace_seconds:
        if not any(_pid_alive(p) for p in children):
            break
        time.sleep(step)
        waited += step
    for pid in children:
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return len(children)


def install_exit_reaper() -> None:
    """Register :func:`reap_child_processes` to run at interpreter exit (fix C).

    Covers exits that bypass uvicorn's lifespan shutdown (an unhandled crash,
    ``sys.exit``); idempotent so repeated startups don't stack handlers."""
    global _exit_reaper_installed
    if _exit_reaper_installed:
        return
    atexit.register(reap_child_processes)
    _exit_reaper_installed = True
