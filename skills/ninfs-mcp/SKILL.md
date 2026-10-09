---
name: using-ninfs-mcp
description: Use when extracting 3DS executable code through the ninfs-mcp MCP server, or when working with 3DS SD, title, or ExeFS data via its detect_sd, mount_sd, find_title, extract_code, or unmount tools.
---

# Using ninfs-mcp

## Overview

ninfs-mcp exposes 3DS SD analysis as five MCP tools. Call the MCP tools —
never `runner` internals. Mounts are read-only; secrets stay masked.

## Flow (in order, no skipping)

1. `detect_sd` — success is `{sd_root, has_n3ds_dir, has_boot9, has_movable}`. `{error: ambiguous}` means ask the user; never guess.
2. `mount_sd` — keep the `mount_id`.
3. `find_title(mount_id, base_id)` — update-first is the default. Use `resolved_title_id` downstream, never the requested ID.
4. `extract_code(title_handle, "<name>/code.bin")` — returns metadata only (path, size, sha256).
5. `unmount(mount_id)` — verify `ok: true, incomplete: false`. Always last, even on failure.

## Rules

- One title per flow. A new title means a new `find_title` (handles are per-title).
- `dest_rel` convention: `<requested-id-lowercase>/code.bin` or `<game-name>/code.bin`.
- Never print or return boot9/movable contents, SD keys, or full secret paths.
- Never skip `unmount`. On failure, unmount anyway, then report `incomplete` plus `errors_sanitized`.

## Common mistakes

- Driving `runner.*` directly (bypasses the sanitizer and response mapping) — use the `server.*` MCP tools.
- Forgetting `unmount` — leaked WinFsp mounts survive the session. Verify `ok`.
- Assuming the base title was used — check `kind` (`base` or `update`).
