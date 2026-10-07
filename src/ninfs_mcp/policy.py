"""Security policy: workspace containment, secrets handling, read-only guard.

No state. All decisions are pure functions so they stay unit-testable.
"""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping


_WINDOWS_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)

_KEY_RE = re.compile(r"(?i)\bkey\s*:\s*[0-9a-f]{16,}\b")
_ID0_RE = re.compile(r"(?i)\bid0\s*:\s*\S+")


def _split_components(dest_rel: str) -> list[str]:
    return [c for c in re.split(r"[\\/]+", dest_rel) if c not in ("", ".")]


def _reject_format(dest_rel: str) -> None:
    if not dest_rel or not dest_rel.strip():
        raise ValueError("empty dest_rel")
    # Absolute / drive / UNC / device paths are never valid workspace-relative input.
    # (Explicit checks: os.path.isabs alone is not airtight for every spelling.)
    drive, _ = os.path.splitdrive(dest_rel)
    if (
        drive
        or dest_rel.startswith("/")
        or dest_rel.startswith("\\")
    ):
        raise ValueError(f"not workspace-relative: {dest_rel!r}")
    parts = _split_components(dest_rel)
    if not parts:
        raise ValueError(f"not workspace-relative: {dest_rel!r}")
    if parts[0].lower() == ".mounts":
        raise ValueError("reserved staging area: .mounts")
    for part in parts:
        if part == "..":
            raise ValueError("parent traversal is never allowed")
        if ":" in part:
            raise ValueError(f"stream/device syntax is never allowed: {part!r}")
        if part != part.strip(" .") or part[-1] in (" ", "."):
            raise ValueError(f"trailing dot/space is never allowed: {part!r}")
        stem = unicodedata.normalize("NFKC", part).split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED:
            raise ValueError(f"reserved device name: {part!r}")


def resolve_dest(
    workspace: Path,
    dest_rel: str,
    secrets: Iterable[str] | None = None,
) -> Path:
    """Resolve dest_rel inside workspace. Raises ValueError on any escape.

    secrets=None collects via get_secrets(). A resolved path identical to a
    secrets file is refused (overwrite protection for key material).
    """
    _reject_format(dest_rel)
    ws_real = os.path.realpath(workspace)
    if not os.path.isdir(ws_real):
        raise ValueError("workspace does not exist")
    candidate = os.path.realpath(os.path.join(ws_real, *_split_components(dest_rel)))
    if os.path.commonpath([ws_real, candidate]) != ws_real:
        raise ValueError("escapes workspace after normalization")
    # Reserved staging area, compared in both resolved and lexical form so a
    # junction at .mounts cannot smuggle writes into the managed area.
    mounts_resolved = os.path.realpath(os.path.join(ws_real, ".mounts"))
    mounts_lexical = os.path.join(ws_real, ".mounts")
    for mounts in (mounts_resolved, mounts_lexical):
        if candidate == mounts or os.path.commonpath([mounts, candidate]) == mounts:
            raise ValueError("reserved staging area: .mounts")
    if secrets is None:
        secrets = get_secrets()
    for secret in secrets:
        if not secret:
            continue
        try:
            if os.path.isfile(secret) and os.path.realpath(secret) == candidate:
                raise ValueError("refusing to overwrite a secrets file")
        except ValueError:
            raise
        except OSError:
            continue
    return Path(candidate)


def check_no_overlap(workspace: Path, sd_root: str) -> None:
    """Refuse any overlap between workspace and the selected SD area."""
    ws_real = os.path.realpath(workspace)
    sd_real = os.path.realpath(sd_root)
    if os.path.splitdrive(ws_real)[0].lower() != os.path.splitdrive(sd_real)[0].lower():
        return  # different drives (or UNC shares) can never overlap
    if ws_real == sd_real or os.path.commonpath([ws_real, sd_real]) in (ws_real, sd_real):
        raise ValueError("workspace overlaps the selected SD area")


def get_secrets(extra: Iterable[str] = ()) -> list[str]:
    """Collect mask targets: env paths + explicit extras, order-preserved."""
    seen: list[str] = []
    for value in (
        os.environ.get("NINFS_BOOT9_PATH", ""),
        os.environ.get("NINFS_MOVABLE_PATH", ""),
        os.environ.get("NINFS_SD_ROOT", ""),
        *extra,
    ):
        if value and value not in seen:
            seen.append(value)
    return seen


def sanitize(text: str, secrets: Iterable[str]) -> str:
    """Mask secret paths, key material and ID0 values. secrets is required.

    Also masks backslash-doubled (traceback/repr) spellings of secrets.
    """
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "[redacted-path]")
            out = out.replace(secret.replace("\\", "\\\\"), "[redacted-path]")
    out = _KEY_RE.sub("[redacted-key]", out)
    out = _ID0_RE.sub("ID0: [redacted-id0]", out)
    return out


def reject_write_args(extra: Mapping[str, Any]) -> None:
    """Refuse any requested write mode. Read-only is enforced, not requested."""
    for key in ("rw", "writable"):
        if extra.get(key):
            raise ValueError(f"write mode requested: {key}")
    for key in ("read_only", "ro"):
        if key in extra and not extra[key]:
            raise ValueError(f"read-only disabled via: {key}")
