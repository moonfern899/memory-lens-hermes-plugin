"""Local debug tests for Memory Lens backend.

Run with:
    cd ~/hermes-hackathon/memory-lens
    HERMES_HOME=/tmp/memlens-test python -m pytest tests/ -v

HERMES_HOME is read from the environment at module import time, so each
test sets it BEFORE importing plugin_api — monkeypatched env var +
importlib.reload give every test a fresh tmp dir for memories, config,
and snapshots.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

# Make `dashboard/plugin_api.py` importable without a package install.
DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))


@pytest.fixture
def tmp_hermes(tmp_path, monkeypatch):
    """Reload plugin_api against a fresh temp HERMES_HOME so each test
    sees pristine memory files / config / snapshot dirs."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Force re-import so module-level path constants pick up the env.
    if "plugin_api" in sys.modules:
        del sys.modules["plugin_api"]
    api = importlib.import_module("plugin_api")
    return tmp_path, api


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def test_parse_entries_empty(tmp_hermes):
    _, api = tmp_hermes
    assert api._parse_entries("") == []
    assert api._parse_entries("   \n  ") == []


def test_parse_entries_single(tmp_hermes):
    _, api = tmp_hermes
    out = api._parse_entries("user prefers methodical, staged turns")
    assert len(out) == 1
    assert out[0]["body"] == "user prefers methodical, staged turns"
    assert out[0]["chars"] == len("user prefers methodical, staged turns")
    assert out[0]["index"] == 0


def test_parse_entries_multiple_with_delim(tmp_hermes):
    _, api = tmp_hermes
    text = "first entry\n§\nsecond entry\n§\nthird entry"
    out = api._parse_entries(text)
    assert [e["body"] for e in out] == ["first entry", "second entry", "third entry"]
    assert [e["index"] for e in out] == [0, 1, 2]


def test_parse_entries_skips_blank_chunks(tmp_hermes):
    _, api = tmp_hermes
    # Empty chunks between delimiters shouldn't become entries.
    text = "\n§\nreal\n§\n\n§\n"
    out = api._parse_entries(text)
    assert [e["body"] for e in out] == ["real"]


# ---------------------------------------------------------------------------
# config + limits
# ---------------------------------------------------------------------------


def test_read_limits_defaults_when_no_config(tmp_hermes):
    _, api = tmp_hermes
    cfg = api._read_memory_config()
    assert cfg["memory_char_limit"] == 2200
    assert cfg["user_char_limit"] == 1375
    assert cfg["memory_enabled"] is True
    assert cfg["provider"] == "builtin"


def test_read_limits_from_config(tmp_hermes):
    tmp, api = tmp_hermes
    (tmp / "config.yaml").write_text(
        "memory:\n"
        "  memory_char_limit: 4400\n"
        "  user_char_limit: 2750\n"
        "  memory_enabled: false\n"
        "  user_profile_enabled: false\n"
        "  provider: honcho\n",
    )
    cfg = api._read_memory_config()
    assert cfg["memory_char_limit"] == 4400
    assert cfg["user_char_limit"] == 2750
    assert cfg["memory_enabled"] is False
    assert cfg["user_profile_enabled"] is False
    assert cfg["provider"] == "honcho"


def test_read_limits_handles_malformed_yaml(tmp_hermes):
    tmp, api = tmp_hermes
    (tmp / "config.yaml").write_text("memory:\n  - this is invalid: yaml: at all")
    cfg = api._read_memory_config()
    # Should fall back to defaults rather than raise.
    assert cfg["memory_char_limit"] == 2200


# ---------------------------------------------------------------------------
# file summary
# ---------------------------------------------------------------------------


def test_file_summary_for_missing_file(tmp_hermes):
    tmp, api = tmp_hermes
    summary = api._file_summary(api.MEMORY_FILE, 2200)
    assert summary["exists"] is False
    assert summary["chars_used"] == 0
    assert summary["pressure"] == 0.0
    assert summary["entries"] == []
    # Path collapses ~ for screenshot privacy.
    assert "Users" not in summary["path"] or summary["path"].startswith("~")


def test_file_summary_with_real_content(tmp_hermes):
    tmp, api = tmp_hermes
    api.MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
    body = "first thing\n§\nsecond thing"
    api.MEMORY_FILE.write_text(body)
    summary = api._file_summary(api.MEMORY_FILE, 100)
    assert summary["exists"] is True
    assert summary["chars_used"] == len(body)
    assert summary["pressure"] == pytest.approx(len(body) / 100)
    assert len(summary["entries"]) == 2


def test_short_home_collapses_username(tmp_hermes):
    _, api = tmp_hermes
    home = str(Path.home())
    assert api._short_home(home + "/.hermes/memories/MEMORY.md") == "~/.hermes/memories/MEMORY.md"
    assert api._short_home("/etc/hermes/x") == "/etc/hermes/x"


def test_short_home_does_not_mangle_prefix_collision(tmp_hermes):
    """A path that starts with the home string but has more chars
    after must NOT be collapsed (e.g. /home/alice-old when home is
    /home/alice)."""
    _, api = tmp_hermes
    home = str(Path.home())
    bad_input = home + "-old/file"
    # Without separator guard this would mangle to ~-old/file.
    assert api._short_home(bad_input) == bad_input


def test_read_memory_config_handles_provider_null(tmp_hermes):
    tmp, api = tmp_hermes
    (tmp / "config.yaml").write_text("memory:\n  provider: null\n")
    cfg = api._read_memory_config()
    # provider: null must NOT render as the literal string "None".
    assert cfg["provider"] == "builtin"


def test_read_memory_config_handles_non_dict_top_level(tmp_hermes):
    tmp, api = tmp_hermes
    (tmp / "config.yaml").write_text("- one\n- two\n")
    cfg = api._read_memory_config()
    assert cfg["memory_char_limit"] == 2200


def test_read_memory_config_handles_non_numeric_limit(tmp_hermes):
    tmp, api = tmp_hermes
    (tmp / "config.yaml").write_text("memory:\n  memory_char_limit: not-a-number\n")
    # Must fall back to defaults rather than raise on int().
    cfg = api._read_memory_config()
    assert cfg["memory_char_limit"] == 2200


def test_parse_entries_indices_skip_no_gaps(tmp_hermes):
    """Malformed file with empty chunks shouldn't leave gaps in
    displayed indices (#1, #3, #5 was the bug)."""
    _, api = tmp_hermes
    text = "first\n§\n\n§\nthird\n§\n\n§\nfifth"
    entries = api._parse_entries(text)
    assert [e["body"] for e in entries] == ["first", "third", "fifth"]
    assert [e["index"] for e in entries] == [0, 1, 2]


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------


def test_take_snapshot_creates_file(tmp_hermes):
    _, api = tmp_hermes
    snap = api._take_snapshot(reason="unit-test")
    assert snap["reason"] == "unit-test"
    saved = api._snapshot_path(snap["ts"])
    assert saved.exists()
    payload = json.loads(saved.read_text())
    assert payload["reason"] == "unit-test"
    assert "memory" in payload and "user" in payload


def test_list_snapshots_returns_sorted(tmp_hermes):
    _, api = tmp_hermes
    # Content must change between snapshots — _take_snapshot now dedups
    # by content hash, so identical-content calls collapse to one.
    api.MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
    api.MEMORY_FILE.write_text("alpha")
    api._take_snapshot("a")
    api.MEMORY_FILE.write_text("beta")
    api._take_snapshot("b")
    api.MEMORY_FILE.write_text("gamma")
    api._take_snapshot("c")
    out = api._list_snapshots()
    # sorted by mtime ascending — earliest first
    assert [s["reason"] for s in out] == ["a", "b", "c"]


def test_take_snapshot_dedups_unchanged_content(tmp_hermes):
    _, api = tmp_hermes
    api.MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
    api.MEMORY_FILE.write_text("same")
    api._take_snapshot("first")
    second = api._take_snapshot("second")
    assert second.get("deduped") is True
    out = api._list_snapshots()
    assert len(out) == 1
    assert out[0]["reason"] == "first"
    # Changing content must allow a new snapshot through.
    api.MEMORY_FILE.write_text("different")
    third = api._take_snapshot("third")
    assert third.get("deduped") is not True
    assert len(api._list_snapshots()) == 2


@pytest.mark.asyncio
async def test_delete_snapshot_removes_file(tmp_hermes):
    _, api = tmp_hermes
    snap = api._take_snapshot("to-delete")
    snap_id = api._snapshot_path(snap["ts"]).stem
    assert api._snapshot_path(snap["ts"]).exists()
    res = await api.delete_snapshot(snap_id)
    assert res["ok"] is True
    assert not api._snapshot_path(snap["ts"]).exists()


@pytest.mark.asyncio
async def test_delete_snapshot_404_for_missing(tmp_hermes):
    from fastapi import HTTPException

    _, api = tmp_hermes
    with pytest.raises(HTTPException) as exc:
        await api.delete_snapshot("9999999.000000")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_snapshot_handles_concurrent_unlink(tmp_hermes):
    """If the auto-prune unlinks a snapshot between exists() and
    unlink(), the route should still 404 cleanly, not 500."""
    from fastapi import HTTPException

    _, api = tmp_hermes
    snap = api._take_snapshot("vanish")
    snap_id = api._snapshot_path(snap["ts"]).stem
    # Simulate the prune unlinking before we call delete_snapshot.
    api._snapshot_path(snap["ts"]).unlink()
    with pytest.raises(HTTPException) as exc:
        await api.delete_snapshot(snap_id)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_raw_content_returns_bytes_verbatim(tmp_hermes):
    """raw-content must return the file char-for-char so the editor
    can round-trip blank lines and trailing whitespace."""
    _, api = tmp_hermes
    api.MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
    quirky = "first\n§\n\n\nsecond entry with trailing spaces   \n"
    api.MEMORY_FILE.write_text(quirky)
    res = await api.raw_content(target="memory")
    assert res["content"] == quirky


@pytest.mark.asyncio
async def test_raw_content_rejects_unknown_target(tmp_hermes):
    from fastapi import HTTPException

    _, api = tmp_hermes
    with pytest.raises(HTTPException) as exc:
        await api.raw_content(target="bogus")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_first_state_does_not_log_external_edit(tmp_hermes):
    """First /state after process start should seed mtimes, not
    snapshot. Otherwise every dashboard restart logs a bogus
    external-edit even though nothing changed."""
    _, api = tmp_hermes
    api.MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
    api.MEMORY_FILE.write_text("preexisting content")
    await api.get_state()
    snaps = api._list_snapshots()
    assert all(s["reason"] != "external-edit" for s in snaps)


@pytest.mark.asyncio
async def test_manual_snapshot_refreshes_mtimes(tmp_hermes):
    """A manual snapshot taken right after an external edit shouldn't
    cause the next /state poll to also fire an external-edit (would
    duplicate)."""
    import time

    _, api = tmp_hermes
    api.MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
    api.MEMORY_FILE.write_text("seed")
    # Seed mtimes via initial /state.
    await api.get_state()
    # External edit.
    time.sleep(0.7)
    api.MEMORY_FILE.write_text("changed by hand")
    # User clicks Snapshot now (before /state would poll).
    await api.create_snapshot()
    before = len(api._list_snapshots())
    # Next /state should NOT add an external-edit on top.
    await api.get_state()
    assert len(api._list_snapshots()) == before


@pytest.mark.asyncio
async def test_delete_snapshot_blocks_path_traversal(tmp_hermes):
    from fastapi import HTTPException

    _, api = tmp_hermes
    for bad in ("../etc/passwd", "..", ".", "a/b", "a\\b"):
        with pytest.raises(HTTPException) as exc:
            await api.delete_snapshot(bad)
        assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# capture endpoint logic (call the route function directly)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_writes_first_entry(tmp_hermes):
    _, api = tmp_hermes
    body = api.CaptureBody(target="memory", content="hello world")
    res = await api.capture(body)
    assert res["ok"] is True
    assert res["chars_used"] == len("hello world\n")
    assert "next session" in res["warning"].lower()
    assert api.MEMORY_FILE.read_text() == "hello world\n"


@pytest.mark.asyncio
async def test_capture_appends_with_delimiter(tmp_hermes):
    _, api = tmp_hermes
    api.MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
    api.MEMORY_FILE.write_text("first\n")
    body = api.CaptureBody(target="memory", content="second")
    await api.capture(body)
    text = api.MEMORY_FILE.read_text()
    assert text == "first\n§\nsecond\n"
    entries = api._parse_entries(text)
    assert [e["body"] for e in entries] == ["first", "second"]


@pytest.mark.asyncio
async def test_capture_flags_over_limit(tmp_hermes):
    _, api = tmp_hermes
    body = api.CaptureBody(target="user", content="x" * 5000)
    res = await api.capture(body)
    # USER.md default limit is 1375
    assert res["over_limit"] is True
    assert res["char_limit"] == 1375


@pytest.mark.asyncio
async def test_capture_rejects_unknown_target(tmp_hermes):
    from fastapi import HTTPException

    _, api = tmp_hermes
    body = api.CaptureBody(target="not-a-thing", content="x")
    with pytest.raises(HTTPException) as exc:
        await api.capture(body)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_capture_rejects_empty(tmp_hermes):
    from fastapi import HTTPException

    _, api = tmp_hermes
    body = api.CaptureBody(target="memory", content="   \n  ")
    with pytest.raises(HTTPException) as exc:
        await api.capture(body)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_capture_rejects_pasted_delimiter(tmp_hermes):
    """A pasted entry containing \\n§\\n would split into multiple
    entries silently. Reject so user knows to use the raw editor."""
    from fastapi import HTTPException

    _, api = tmp_hermes
    body = api.CaptureBody(target="memory", content="entry one\n§\nentry two")
    with pytest.raises(HTTPException) as exc:
        await api.capture(body)
    assert exc.value.status_code == 400
    assert "delimiter" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_capture_preserves_leading_trailing_spaces(tmp_hermes):
    """The parser preserves intra-entry whitespace, so capture must
    too — only newlines should be stripped at the boundary."""
    _, api = tmp_hermes
    body = api.CaptureBody(target="memory", content="  spaces preserved  ")
    await api.capture(body)
    text = api.MEMORY_FILE.read_text()
    assert "  spaces preserved  " in text


# ---------------------------------------------------------------------------
# raw-write endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_write_replaces_file(tmp_hermes):
    _, api = tmp_hermes
    api.MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
    api.MEMORY_FILE.write_text("old content\n§\nold second")
    body = api.RawWriteBody(target="memory", content="brand new")
    res = await api.raw_write(body)
    assert res["ok"] is True
    # Trailing newline normalized.
    assert api.MEMORY_FILE.read_text() == "brand new\n"
    assert res["chars_used"] == len("brand new\n")


@pytest.mark.asyncio
async def test_raw_write_takes_safety_snapshot(tmp_hermes):
    _, api = tmp_hermes
    api.MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
    api.MEMORY_FILE.write_text("content i'd like to keep recoverable")
    body = api.RawWriteBody(target="memory", content="overwritten")
    await api.raw_write(body)
    snaps = api._list_snapshots()
    reasons = [s["reason"] for s in snaps]
    assert any(r and r.startswith("pre-raw-write") for r in reasons)


@pytest.mark.asyncio
async def test_raw_write_flags_over_limit(tmp_hermes):
    _, api = tmp_hermes
    body = api.RawWriteBody(target="user", content="z" * 5000)
    res = await api.raw_write(body)
    assert res["over_limit"] is True


@pytest.mark.asyncio
async def test_raw_write_rejects_unknown_target(tmp_hermes):
    from fastapi import HTTPException

    _, api = tmp_hermes
    body = api.RawWriteBody(target="elsewhere", content="x")
    with pytest.raises(HTTPException) as exc:
        await api.raw_write(body)
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# state route — full integration on the function level
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_edit_triggers_auto_snapshot(tmp_hermes):
    """Simulate a user editing MEMORY.md directly with vim / a finder
    file edit, and confirm the auto-watcher catches it on the next
    /state poll."""
    import time

    _, api = tmp_hermes
    api.MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
    api.MEMORY_FILE.write_text("original text\n")

    # First /state ONLY seeds _last_seen_mtimes (no snapshot — that
    # was a bogus "external-edit" on every dashboard restart).
    await api.get_state()
    initial_count = len(api._list_snapshots())

    # External edit: simulate vim writing the file. mtime needs to be
    # at least 0.6s newer to clear the 0.5s debounce in the watcher.
    time.sleep(0.7)
    api.MEMORY_FILE.write_text("hand-edited via vim\n§\nadded a second entry")

    # Next poll should detect the change and snapshot it.
    await api.get_state()
    snaps = api._list_snapshots()
    assert len(snaps) > initial_count
    latest = snaps[-1]
    assert latest["reason"] == "external-edit"
    # Snapshot should reflect the new content, not the old.
    full = json.loads(api._snapshot_path(latest["ts"]).read_text())
    assert "hand-edited via vim" in full["memory"]["entries"][0]["body"]


@pytest.mark.asyncio
async def test_plugin_writes_dont_double_snapshot(tmp_hermes):
    """After a plugin-side write, the next /state poll should NOT
    add an auto:agent-wrote snapshot — _refresh_seen_mtimes prevents
    the auto-watcher from re-firing on our own change."""
    _, api = tmp_hermes

    body = api.CaptureBody(target="memory", content="captured by plugin")
    await api.capture(body)
    after_capture = len(api._list_snapshots())

    # Several /state polls in a row — none should produce extra snapshots.
    await api.get_state()
    await api.get_state()
    await api.get_state()
    assert len(api._list_snapshots()) == after_capture


@pytest.mark.asyncio
async def test_get_state_smoke(tmp_hermes):
    _, api = tmp_hermes
    api.MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
    api.MEMORY_FILE.write_text("a\n§\nb")
    state = await api.get_state()
    # chars_used = raw file length (5 = "a" + "\n§\n" + "b").
    assert state["memory"]["chars_used"] == 5
    assert len(state["memory"]["entries"]) == 2
    assert state["limits"]["memory"] == 2200
    assert state["config"]["memory_enabled"] is True
    assert state["hermes_home"].startswith("~") or "/" in state["hermes_home"]
