"""Stdio MCP server: five tools only. Maps runner internals to public dicts.

Runner calls run in worker threads so the event loop stays responsive;
cancellation is delivered at await boundaries. Internal objects never
leave this module: only IDs cross into workers, resolution happens
inside the worker under the runner lock.
"""
from __future__ import annotations

import argparse
import atexit
import functools
import os
from typing import Any, Callable

import anyio
import anyio.to_thread
from mcp.server.mcpserver import MCPServer

from . import runner
from .policy import get_secrets, resolve_dest, sanitize
from .runner import RunnerError

mcp = MCPServer("ninfs-mcp")
_ATEXIT_REGISTERED = False


def _error_dict(error: RunnerError) -> dict:
    return {"error": error.code, "message_sanitized": error.message_sanitized,
            "incomplete": error.incomplete}


def _mapped(fn: Callable) -> Callable:
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> dict:
        try:
            return await fn(*args, **kwargs)
        except RunnerError as e:
            # Second net: runner sanitizes at raise time; re-mask with env
            # secrets here in case a raw string slipped through.
            return {"error": e.code,
                    "message_sanitized": sanitize(e.message_sanitized, get_secrets()),
                    "incomplete": e.incomplete}
        except Exception as e:  # noqa: BLE001 - last resort, sanitized
            return {"error": "internal",
                    "message_sanitized": sanitize(type(e).__name__, get_secrets()),
                    "incomplete": False}

    return wrapper


async def _run_worker(fn: Callable, *args: Any) -> Any:
    return await anyio.to_thread.run_sync(functools.partial(fn, *args))


def _resolve_session(mount_id: str):
    session = runner._SESSIONS.get(mount_id)
    if session is None:
        raise RunnerError("unknown-id", sanitize(f"unknown mount id", get_secrets()))
    return session


def _do_find_title(mount_id: str, title_id: str):
    with runner._LOCK:
        return runner.find_title(_resolve_session(mount_id), title_id)


def _do_extract(title_handle: str, dest_rel: str):
    with runner._LOCK:
        target = None
        for session in runner._SESSIONS.values():
            if title_handle in session.title_handles:
                target = session.title_handles[title_handle]
                break
        if target is None:
            raise RunnerError("unknown-handle", "unknown or expired title handle")
        return runner.extract_code(target, dest_rel)


@mcp.tool()
@_mapped
async def detect_sd() -> dict:
    """Detect the 3DS SD card. Existence flags only, never file contents."""
    return await _run_worker(runner.detect_sd)


@mcp.tool()
@_mapped
async def mount_sd() -> dict:
    """Mount the SD card read-only. Returns mount_id + mount_point."""
    session = await _run_worker(runner.mount_sd)
    sd_point = session.staging
    for child in session.children:
        if child.kind == "sd":
            sd_point = child.mount_point
            break
    return {"mount_id": session.mount_id, "mount_point": sd_point}


@mcp.tool()
@_mapped
async def find_title(mount_id: str, title_id: str) -> dict:
    """Resolve a title (update-first) and mount down to ExeFS."""
    handle = await _run_worker(_do_find_title, mount_id, title_id)
    return {"requested_title_id": handle.requested_title_id,
            "resolved_title_id": handle.resolved_title_id,
            "kind": handle.kind, "tmd_found": handle.tmd_found,
            "title_handle": handle.handle_id}


@mcp.tool()
@_mapped
async def extract_code(title_handle: str, dest_rel: str) -> dict:
    """Copy the decompressed code into the workspace. Metadata only."""
    resolve_dest(_workspace_dir(), dest_rel, get_secrets())  # pre-validate before runner
    out = await _run_worker(_do_extract, title_handle, dest_rel)
    return {"dest_path": out.dest_path, "size": out.size,
            "sha256": out.sha256, "code_entry": out.code_entry}


@mcp.tool()
@_mapped
async def unmount(mount_id: str) -> dict:
    """Reverse-clean a mount session plus residual sweep."""
    summary = await _run_worker(runner.unmount, mount_id)
    return {"ok": summary.ok, "incomplete": summary.incomplete,
            "errors_sanitized": summary.errors_sanitized}


def _workspace_dir() -> str:
    return os.environ.get("NINFS_WORKSPACE", "")


def startup() -> dict:
    """Validate env, create workspace, gate WinFsp, sweep residuals."""
    global _ATEXIT_REGISTERED
    ws = _workspace_dir()
    if not ws or not os.path.isabs(ws):
        raise RunnerError("workspace-misconfigured", "NINFS_WORKSPACE must be an absolute path")
    for var in ("NINFS_BOOT9_PATH", "NINFS_MOVABLE_PATH"):
        if not os.environ.get(var):
            raise RunnerError("secrets-misconfigured", sanitize(f"{var} is not set", get_secrets()))
    os.makedirs(ws, exist_ok=True)
    runner.check_winfs()
    report = runner.retry_residuals()
    if not _ATEXIT_REGISTERED:
        atexit.register(cleanup_all)
        _ATEXIT_REGISTERED = True
    return {"workspace": ws, "residuals_recovered": report.recovered,
            "residuals_remaining": report.remaining}


def cleanup_all() -> None:
    """Best-effort cleanup of every session and residual. Never raises."""
    try:
        for mount_id in list(runner._SESSIONS.keys()):
            try:
                runner.unmount(mount_id)
            except Exception:
                pass
        try:
            runner.retry_residuals()
        except Exception:
            pass
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ninfs-mcp", description=__doc__)
    parser.parse_args(argv)
    startup()
    try:
        mcp.run(transport="stdio")
    finally:
        cleanup_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
