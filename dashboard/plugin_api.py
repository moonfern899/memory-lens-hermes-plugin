"""Memory Lens — backend API.

Reads the two flat memory files Hermes maintains (MEMORY.md, USER.md),
parses them into individual entries, and exposes char-limit pressure,
entry breakdowns, snapshot history, and a guarded write endpoint.

Hermes' memory architecture, summarised so the rest of the file makes
sense:
  - Entries live in two files at ~/.hermes/memories/{MEMORY,USER}.md
  - Multiple entries per file, separated by `\\n§\\n`
  - Hard char limits per file (default 2200 / 1375), configurable
  - Reads happen once at session start (frozen-snapshot); writes hit
    disk immediately but only show up next session
  - Writes normally go through the agent's `memory` tool — we use the
    same fcntl lock + atomic temp-file replace pattern for safety
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Iterator

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Mirror Hermes' write pattern: acquire an exclusive flock on a
    sidecar .lock file before mutating the target. Prevents races with
    the agent's memory tool, which uses the same convention."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

# Hermes uses ~/.hermes by default; respect HERMES_HOME if set.
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
MEMORIES_DIR = HERMES_HOME / "memories"
MEMORY_FILE = MEMORIES_DIR / "MEMORY.md"
USER_FILE = MEMORIES_DIR / "USER.md"
CONFIG_FILE = HERMES_HOME / "config.yaml"

# Snapshots live next to the plugin so they don't pollute Hermes' state.
PLUGIN_DATA_DIR = HERMES_HOME / "plugins" / "memory-lens" / "data"
SNAPSHOTS_DIR = PLUGIN_DATA_DIR / "snapshots"
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# Defaults straight from hermes-agent/tools/memory_tool.py.
DEFAULT_MEMORY_LIMIT = 2200
DEFAULT_USER_LIMIT = 1375

# The literal section delimiter the agent writes between entries.
ENTRY_DELIM = "\n§\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_file(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Hermes silently treats unreadable files as empty
        return ""


def _parse_entries(text: str) -> list[dict[str, Any]]:
    """Split a memory file by the section delimiter and tag each entry.
    Indices are sequential over kept entries (a malformed file with
    blank chunks won't leave gaps in the displayed badges)."""
    if not text.strip():
        return []
    entries: list[dict[str, Any]] = []
    for body in text.split(ENTRY_DELIM):
        body = body.strip("\n")
        if not body:
            continue
        entries.append(
            {
                "index": len(entries),
                "body": body,
                "chars": len(body),
                "lines": body.count("\n") + 1,
                "preview": body[:140],
            }
        )
    return entries


def _read_memory_config() -> dict[str, Any]:
    """Pull the full memory config block. Falls back to safe defaults
    when config.yaml is missing, malformed, or contains unexpected
    types (e.g. a non-numeric char-limit, a top-level non-mapping)."""
    defaults = {
        "memory_char_limit": DEFAULT_MEMORY_LIMIT,
        "user_char_limit": DEFAULT_USER_LIMIT,
        "memory_enabled": True,
        "user_profile_enabled": True,
        "provider": "builtin",
    }
    if not CONFIG_FILE.exists():
        return defaults
    try:
        data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return defaults
        mem = data.get("memory") or {}
        if not isinstance(mem, dict):
            return defaults
        # `or` instead of `get(..., default)` so `provider: null` doesn't
        # render as the literal string "None".
        return {
            "memory_char_limit": int(mem.get("memory_char_limit") or DEFAULT_MEMORY_LIMIT),
            "user_char_limit": int(mem.get("user_char_limit") or DEFAULT_USER_LIMIT),
            "memory_enabled": bool(mem.get("memory_enabled", True)),
            "user_profile_enabled": bool(mem.get("user_profile_enabled", True)),
            "provider": str(mem.get("provider") or "builtin"),
        }
    except (yaml.YAMLError, ValueError, TypeError):
        return defaults


def _read_limits() -> dict[str, int]:
    cfg = _read_memory_config()
    return {"memory": cfg["memory_char_limit"], "user": cfg["user_char_limit"]}


def _short_home(path: Path | str) -> str:
    """Render an absolute path with the home dir collapsed to ~ —
    avoids leaking the username in screenshots. Requires a path
    separator after the home prefix so /home/alice-old/... isn't
    mangled into ~-old/...."""
    s = str(path)
    home = str(Path.home())
    if s == home:
        return "~"
    prefix = home + os.sep
    if s.startswith(prefix):
        return "~" + os.sep + s[len(prefix):]
    return s


def _file_summary(path: Path, limit: int) -> dict[str, Any]:
    text = _read_file(path)
    entries = _parse_entries(text)
    used = len(text)
    return {
        "path": _short_home(path),
        "exists": path.exists(),
        "mtime": path.stat().st_mtime if path.exists() else None,
        "chars_used": used,
        "char_limit": limit,
        "pressure": (used / limit) if limit else 0.0,
        "entries": entries,
    }


# ---------------------------------------------------------------------------
# Snapshot machinery
# ---------------------------------------------------------------------------


def _snapshot_path(ts: float) -> Path:
    # Microsecond resolution — without this, two snapshots taken in the
    # same wall-clock second collide and overwrite each other.
    return SNAPSHOTS_DIR / f"{ts:.6f}.json"


def _content_hash(memory_text: str, user_text: str) -> str:
    h = hashlib.sha256()
    h.update(memory_text.encode("utf-8"))
    h.update(b"\0")
    h.update(user_text.encode("utf-8"))
    return h.hexdigest()


def _latest_snapshot_hash() -> str | None:
    files = sorted(SNAPSHOTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text()).get("content_hash")
    except (json.JSONDecodeError, OSError):
        return None


def _take_snapshot(reason: str = "manual") -> dict[str, Any]:
    """Capture the current state of both memory files."""
    limits = _read_limits()
    memory_text = _read_file(MEMORY_FILE)
    user_text = _read_file(USER_FILE)
    chash = _content_hash(memory_text, user_text)
    # Content-hash dedup: if nothing actually changed since the last
    # snapshot, skip the write. Catches both idle agents touching mtime
    # AND the pre-raw-write/raw-write pair when an edit is a no-op.
    if chash == _latest_snapshot_hash():
        return {"ts": time.time(), "reason": reason, "deduped": True, "content_hash": chash}
    snap = {
        "ts": time.time(),
        "reason": reason,
        "content_hash": chash,
        "memory": _file_summary(MEMORY_FILE, limits["memory"]),
        "user": _file_summary(USER_FILE, limits["user"]),
    }
    _snapshot_path(snap["ts"]).write_text(json.dumps(snap, indent=2))
    # Keep snapshots bounded so we don't grow forever — newest 200.
    files = sorted(SNAPSHOTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    for old in files[:-200]:
        old.unlink(missing_ok=True)
    return snap


def _list_snapshots() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in sorted(SNAPSHOTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            data = json.loads(p.read_text())
            out.append(
                {
                    "id": p.stem,
                    "ts": data.get("ts"),
                    "reason": data.get("reason"),
                    "memory_chars": data.get("memory", {}).get("chars_used", 0),
                    "user_chars": data.get("user", {}).get("chars_used", 0),
                }
            )
        except (json.JSONDecodeError, OSError):
            continue
    return out


# ---------------------------------------------------------------------------
# Auto-snapshot on file change (cheap mtime poll on every state read)
# ---------------------------------------------------------------------------

_last_seen_mtimes: dict[str, float] = {}


def _refresh_seen_mtimes() -> None:
    """Mark the current on-disk mtimes as 'already seen' so the next
    auto-watcher tick doesn't re-snapshot a change we already captured
    explicitly."""
    for label, path in (("memory", MEMORY_FILE), ("user", USER_FILE)):
        if path.exists():
            _last_seen_mtimes[label] = path.stat().st_mtime


def _maybe_auto_snapshot() -> None:
    """If either file changed since last call, snapshot it. Catches
    writes that didn't go through the plugin (i.e. the Hermes agent's
    own memory tool). Plugin-initiated writes call _refresh_seen_mtimes
    so this path doesn't double-snapshot them.

    On first call after process start, only seed the seen-mtimes table
    — don't snapshot. Otherwise every dashboard restart would log a
    bogus external-edit even though nothing actually changed."""
    if not _last_seen_mtimes:
        _refresh_seen_mtimes()
        return
    changed = False
    for label, path in (("memory", MEMORY_FILE), ("user", USER_FILE)):
        if not path.exists():
            continue
        mtime = path.stat().st_mtime
        last = _last_seen_mtimes.get(label)
        if last is None or mtime > last + 0.5:
            _last_seen_mtimes[label] = mtime
            changed = True
    if changed:
        _take_snapshot(reason="external-edit")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

router = APIRouter()


@router.get("/state")
async def get_state() -> dict[str, Any]:
    """Live state of both memory files + char-limit pressure + the
    memory-related config flags so the UI can warn about disabled
    memory or non-builtin providers."""
    _maybe_auto_snapshot()
    cfg = _read_memory_config()
    limits = {"memory": cfg["memory_char_limit"], "user": cfg["user_char_limit"]}
    return {
        "memory": _file_summary(MEMORY_FILE, limits["memory"]),
        "user": _file_summary(USER_FILE, limits["user"]),
        "limits": limits,
        "hermes_home": _short_home(HERMES_HOME),
        "config": {
            "memory_enabled": cfg["memory_enabled"],
            "user_profile_enabled": cfg["user_profile_enabled"],
            "provider": cfg["provider"],
        },
    }


@router.get("/history")
async def get_history() -> dict[str, Any]:
    return {"snapshots": _list_snapshots()}


@router.get("/snapshot/{snap_id}")
async def get_snapshot(snap_id: str) -> dict[str, Any]:
    p = SNAPSHOTS_DIR / f"{snap_id}.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return json.loads(p.read_text())


@router.post("/snapshot")
async def create_snapshot() -> dict[str, Any]:
    snap = _take_snapshot(reason="manual")
    # If a manual snapshot fires right after an external edit, the auto-
    # watcher would otherwise produce a duplicate external-edit on the
    # next /state poll. Seed mtimes so it doesn't.
    _refresh_seen_mtimes()
    return snap


@router.delete("/snapshot/{snap_id}")
async def delete_snapshot(snap_id: str) -> dict[str, Any]:
    """Remove one snapshot by id. id is the basename of the file
    without the .json extension (the timestamp). Path-traversal is
    blocked by reconstructing the path inside SNAPSHOTS_DIR."""
    # Reject anything that would escape the snapshots dir.
    if "/" in snap_id or "\\" in snap_id or snap_id in ("..", "."):
        raise HTTPException(status_code=400, detail="invalid snapshot id")
    p = SNAPSHOTS_DIR / f"{snap_id}.json"
    # exists() + unlink() races with the auto-prune. unlink(missing_ok=
    # True) collapses the check into the operation. We still want the
    # 404 contract for "really wasn't there" so check after.
    existed = p.exists()
    p.unlink(missing_ok=True)
    if not existed:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return {"ok": True, "deleted": snap_id}


class CaptureBody(BaseModel):
    target: str  # "memory" or "user"
    content: str


@router.post("/capture")
async def capture(body: CaptureBody) -> dict[str, Any]:
    """Append a new entry to MEMORY.md or USER.md.

    NOTE: Hermes only reads memory at session start, so this entry will
    NOT be visible to the currently running session — but it WILL be
    available next session. The frontend surfaces this caveat.
    """
    if body.target not in ("memory", "user"):
        raise HTTPException(status_code=400, detail="target must be 'memory' or 'user'")
    # Only trim newlines so the parser preserves leading/trailing
    # spaces the user actually typed.
    content = body.content.strip("\n")
    if not content.strip():
        raise HTTPException(status_code=400, detail="content is empty")
    # A pasted entry containing the section-sign delimiter would split
    # into multiple entries silently — reject and let the user use the
    # raw editor (where multi-entry text is the explicit contract).
    if ENTRY_DELIM in content or content.strip() == "§":
        raise HTTPException(
            status_code=400,
            detail="content contains the entry delimiter (\\n§\\n) — "
                   "use the raw editor for multi-entry edits.",
        )

    target_path = MEMORY_FILE if body.target == "memory" else USER_FILE
    limits = _read_limits()
    limit = limits[body.target]

    MEMORIES_DIR.mkdir(parents=True, exist_ok=True)

    # Read-modify-write under the lock so a concurrent agent write
    # can't slip between our read and our overwrite.
    with _file_lock(target_path):
        existing = _read_file(target_path)
        if existing.strip():
            new_text = existing.rstrip("\n") + ENTRY_DELIM + content + "\n"
        else:
            new_text = content + "\n"
        over_limit = len(new_text) > limit
        tmp_path = target_path.with_suffix(target_path.suffix + ".memlens.tmp")
        tmp_path.write_text(new_text, encoding="utf-8")
        os.replace(tmp_path, target_path)

    _take_snapshot(reason=f"capture:{body.target}")
    _refresh_seen_mtimes()  # don't let the auto-watcher double-snapshot
    return {
        "ok": True,
        "chars_used": len(new_text),
        "char_limit": limit,
        "over_limit": over_limit,
        "warning": (
            "Hermes only reads memory at session start — this entry will "
            "be visible next session, not the current one."
        ),
    }


class RawWriteBody(BaseModel):
    target: str  # "memory" or "user"
    content: str


@router.post("/raw-write")
async def raw_write(body: RawWriteBody) -> dict[str, Any]:
    """Overwrite the entire MEMORY.md or USER.md file.

    Heavier hammer than /capture — replaces all entries instead of
    appending one. A safety snapshot is taken inside the lock before
    overwriting, so the previous version is always recoverable.
    """
    if body.target not in ("memory", "user"):
        raise HTTPException(status_code=400, detail="target must be 'memory' or 'user'")

    target_path = MEMORY_FILE if body.target == "memory" else USER_FILE
    limits = _read_limits()
    limit = limits[body.target]

    new_text = body.content
    # Normalize: ensure trailing newline (matches what the agent writes).
    if new_text and not new_text.endswith("\n"):
        new_text = new_text + "\n"

    # Two-snapshot pattern for raw-write, both INSIDE the lock so an
    # agent write can't slip in between the snapshot and the overwrite
    # (which would silently lose the agent's write from history):
    #   pre-raw-write:X = state RIGHT before we overwrite (recovery point)
    #   raw-write:X     = state AFTER the overwrite (new baseline)
    MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
    with _file_lock(target_path):
        if target_path.exists():
            _take_snapshot(reason=f"pre-raw-write:{body.target}")
        tmp_path = target_path.with_suffix(target_path.suffix + ".memlens.tmp")
        tmp_path.write_text(new_text, encoding="utf-8")
        os.replace(tmp_path, target_path)
        _take_snapshot(reason=f"raw-write:{body.target}")

    _refresh_seen_mtimes()
    return {
        "ok": True,
        "chars_used": len(new_text),
        "char_limit": limit,
        "over_limit": len(new_text) > limit,
        "warning": (
            "Hermes only reads memory at session start — your edits will "
            "apply on the next session, not the current one."
        ),
    }


@router.get("/raw-content")
async def raw_content(target: str) -> dict[str, Any]:
    """Return the file contents byte-for-byte (well, char-for-char).
    The /state endpoint returns parsed entries — re-joining those would
    lose blank lines / trailing whitespace, so the raw editor reads
    from here instead to round-trip correctly."""
    if target not in ("memory", "user"):
        raise HTTPException(status_code=400, detail="target must be 'memory' or 'user'")
    path = MEMORY_FILE if target == "memory" else USER_FILE
    return {"target": target, "content": _read_file(path), "exists": path.exists()}


@router.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "memories_dir": str(MEMORIES_DIR),
        "memory_exists": MEMORY_FILE.exists(),
        "user_exists": USER_FILE.exists(),
    }
