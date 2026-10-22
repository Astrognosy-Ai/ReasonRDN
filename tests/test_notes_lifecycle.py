"""Tests for 0.6.3: search over every note, forget, update, date filters, and
turning node lookup off.

Every test works in a temporary folder (see conftest.py), never in a real store.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import rdn
import rdn.client as client_mod
from rdn import notes
from rdn.client import RDNClient
from rdn.node.server import PrivateWARFNodeServer, PrivateWARFRequestHandler, default_db_path

OLD_NOTE = "Zebra crossing rollout was approved by the safety board"
QUERY = "zebra crossing"


def _local_client(db_path: Path) -> RDNClient:
    client = RDNClient(db_path=db_path)
    assert client.node_url is None
    return client


def _fill(client: RDNClient, count: int = 250) -> None:
    for i in range(count):
        result = client.remember(f"Routine filler note number {i} about weekly status", tags=["filler"])
        assert result["status"] == "remembered"


def _set_saved_at(db_path: Path, address: str, when: datetime) -> None:
    conn = sqlite3.connect(str(db_path))
    with conn:
        conn.execute(
            "UPDATE warf_artifacts SET deposited_at = ? WHERE address = ?",
            (when.astimezone(timezone.utc).isoformat(), address),
        )
    conn.close()


def _old_note_store(db_path: Path) -> RDNClient:
    """One old note, then 250 newer notes."""
    client = _local_client(db_path)
    saved = client.remember(OLD_NOTE, tags=["safety"])
    _set_saved_at(db_path, saved["address"], datetime.now(timezone.utc) - timedelta(days=400))
    _fill(client)
    return client


# --------------------------------------------------------------------------
# Recall searches every note
# --------------------------------------------------------------------------


def test_old_note_found_among_250_newer_notes_with_default_limit(tmp_path) -> None:
    client = _old_note_store(tmp_path / "rdn.db")
    hits = client.recall(QUERY)  # default limit
    assert hits and hits[0]["content"] == OLD_NOTE
    assert rdn.RDNClient(db_path=tmp_path / "rdn.db").recall("zebra")[0]["content"] == OLD_NOTE
    conn = sqlite3.connect(str(tmp_path / "rdn.db"))
    try:
        assert notes.search_index_ready(conn)
    finally:
        conn.close()


def test_old_note_found_with_module_level_recall(tmp_path) -> None:
    client = _old_note_store(client_mod.DEFAULT_DB_PATH)
    assert client.db_path == client_mod.DEFAULT_DB_PATH
    hits = rdn.recall(QUERY)
    assert [h["content"] for h in hits][:1] == [OLD_NOTE]


def test_old_note_found_without_full_text_index(tmp_path, monkeypatch) -> None:
    """Fallback when SQLite has no FTS5: every note is read and checked."""
    monkeypatch.setattr(notes, "_ensure_search_index", lambda conn: None)
    client = _old_note_store(tmp_path / "rdn.db")
    conn = sqlite3.connect(str(tmp_path / "rdn.db"))
    try:
        assert not notes.search_index_ready(conn)
    finally:
        conn.close()
    assert client.recall(QUERY)[0]["content"] == OLD_NOTE


def test_short_words_search_every_note(tmp_path) -> None:
    """Words under 3 letters cannot use the index, so every note is checked."""
    client = _local_client(tmp_path / "rdn.db")
    saved = client.remember("Rack q7 holds the backup disks")
    _set_saved_at(tmp_path / "rdn.db", saved["address"], datetime.now(timezone.utc) - timedelta(days=30))
    _fill(client)
    assert client.recall("q7")[0]["content"] == "Rack q7 holds the backup disks"


def test_index_is_built_for_an_older_database(tmp_path) -> None:
    """A 0.6.2 database (no index, no status columns) is upgraded on open."""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE warf_artifacts (
            artifact_id TEXT PRIMARY KEY, address TEXT NOT NULL UNIQUE, domain TEXT NOT NULL,
            category TEXT NOT NULL, task TEXT NOT NULL, deposited_at TEXT NOT NULL,
            audit_hash TEXT NOT NULL, metadata_json TEXT NOT NULL
        )
        """
    )
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for i in range(260):
        content = OLD_NOTE if i == 0 else f"Older filler {i}"
        address = f"reason://legacy/handoff/h-{i:016x}"
        conn.execute(
            "INSERT INTO warf_artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"{i:040x}",
                address,
                "legacy",
                "handoff",
                address.rsplit("/", 1)[-1],
                (base + timedelta(minutes=i)).isoformat(),
                "0" * 64,
                json.dumps({"content": content, "tags": []}),
            ),
        )
    conn.commit()
    conn.close()

    client = _local_client(db_path)
    assert client.recall(QUERY)[0]["content"] == OLD_NOTE
    conn = sqlite3.connect(str(db_path))
    try:
        assert notes.search_index_ready(conn)
        indexed = conn.execute("SELECT COUNT(*) FROM rdn_note_search").fetchone()[0]
        assert indexed == 260
        columns = {row[1] for row in conn.execute("PRAGMA table_info(warf_artifacts)")}
        assert {"forgotten_at", "superseded_by"} <= columns
    finally:
        conn.close()


def test_index_follows_replace_and_delete(tmp_path) -> None:
    db_path = tmp_path / "rdn.db"
    client = _local_client(db_path)
    first = client.remember("Kestrel nest box number one", reason_address="reason://birds/boxes/one")
    client.remember("Kestrel nest box number one, cleaned", reason_address="reason://birds/boxes/one")
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT COUNT(*) FROM warf_artifacts").fetchone()[0]
        indexed = conn.execute("SELECT COUNT(*) FROM rdn_note_search").fetchone()[0]
        assert rows == indexed == 1
    finally:
        conn.close()
    assert [h["content"] for h in client.recall("kestrel")] == ["Kestrel nest box number one, cleaned"]
    client.forget(first["address"], purge=True)
    assert client.recall("kestrel") == []
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM rdn_note_search").fetchone()[0] == 0
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Forget
# --------------------------------------------------------------------------


def test_forget_hides_and_purge_deletes(tmp_path) -> None:
    db_path = tmp_path / "rdn.db"
    client = _local_client(db_path)
    saved = client.remember("The staging password lives in the team vault")
    client.remember("Unrelated note about the team lunch")

    hit = client.recall("staging vault")[0]
    assert hit["id"] == saved["artifact_id"]

    result = client.forget(saved["artifact_id"][:8])
    assert result["status"] == "forgotten" and result["purged"] is False
    assert client.recall("staging vault") == []
    assert client.list_prefix("reason://astrognosy") and all(
        r["content"] != "The staging password lives in the team vault"
        for r in client.list_prefix("reason://astrognosy")
    )
    assert client.resolve(saved["address"]) is None
    hidden = client.recall("staging vault", include_hidden=True)
    assert hidden[0]["state"] == "forgotten"

    # Forgetting twice is fine; purge still finds the hidden note.
    assert client.forget(saved["address"])["status"] == "forgotten"
    assert client.forget(saved["address"], purge=True)["status"] == "purged"
    assert client.recall("staging vault", include_hidden=True) == []
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM warf_artifacts WHERE address = ?", (saved["address"],)
        ).fetchone()[0] == 0
    finally:
        conn.close()
    with pytest.raises(rdn.NoteLookupError):
        client.forget(saved["address"])


def test_remembering_the_same_text_again_brings_a_note_back(tmp_path) -> None:
    client = _local_client(tmp_path / "rdn.db")
    saved = client.remember("Coffee machine is on floor two")
    client.forget(saved["artifact_id"])
    assert client.recall("coffee machine") == []
    client.remember("Coffee machine is on floor two")
    assert client.recall("coffee machine")[0]["content"] == "Coffee machine is on floor two"


def test_note_ids_must_be_clear(tmp_path) -> None:
    db_path = tmp_path / "rdn.db"
    client = _local_client(db_path)
    conn = sqlite3.connect(str(db_path))
    for suffix in ("1", "2"):
        record = list(notes.build_note(f"Twin note {suffix}", project="twins"))
        record[0] = "abcdef" + suffix * 34
        with conn:
            notes.insert_note(conn, tuple(record))
    conn.close()
    with pytest.raises(rdn.NoteLookupError, match="more than one"):
        client.forget("abcdef")
    with pytest.raises(rdn.NoteLookupError, match="too short"):
        client.forget("abc")
    with pytest.raises(rdn.NoteLookupError, match="not a note id"):
        client.forget("hello")
    with pytest.raises(rdn.NoteLookupError, match="No saved note"):
        client.forget("reason://nobody/handoff/h-0000000000000000")
    assert client.forget("abcdef1")["status"] == "forgotten"


# --------------------------------------------------------------------------
# Update
# --------------------------------------------------------------------------


def test_update_supersedes_and_keeps_old_version(tmp_path) -> None:
    client = _local_client(tmp_path / "rdn.db")
    old = client.remember("Deploys happen on Tuesdays", tags=["deploy"], project="web", meta={"owner": "ops"})

    new = client.update(old["artifact_id"], "Deploys happen on Thursdays")
    assert new["status"] == "updated"
    assert new["replaces"] == old["address"]
    assert new["address"] != old["address"]

    hits = client.recall("deploys happen")
    assert [h["content"] for h in hits] == ["Deploys happen on Thursdays"]
    assert hits[0]["tags"] == ["deploy"]
    assert hits[0]["project"] == "web"
    assert hits[0]["meta"]["supersedes"] == old["address"]
    assert hits[0]["meta"]["owner"] == "ops"

    everything = client.recall("deploys happen", include_hidden=True)
    old_hit = [h for h in everything if h["address"] == old["address"]][0]
    assert old_hit["state"] == "superseded" and old_hit["superseded_by"] == new["address"]

    with pytest.raises(rdn.NoteLookupError, match="already replaced"):
        client.update(old["artifact_id"], "Deploys happen on Fridays")
    assert client.update(new["id"], "Deploys happen on Thursdays")["status"] == "unchanged"

    # The old version can still be deleted for good.
    assert client.forget(old["address"], purge=True)["status"] == "purged"
    assert [h["content"] for h in client.recall("deploys happen", include_hidden=True)] == [
        "Deploys happen on Thursdays"
    ]


def test_update_rejects_empty_text(tmp_path) -> None:
    client = _local_client(tmp_path / "rdn.db")
    old = client.remember("Something to change")
    with pytest.raises(ValueError, match="empty"):
        client.update(old["artifact_id"], "   ")


def test_module_level_forget_and_update() -> None:
    saved = rdn.remember("Lunch is at noon on Fridays")
    newer = rdn.update(saved["artifact_id"], "Lunch is at one on Fridays")
    assert [h["content"] for h in rdn.recall("lunch fridays")] == ["Lunch is at one on Fridays"]
    rdn.forget(newer["id"])
    assert rdn.recall("lunch fridays") == []


# --------------------------------------------------------------------------
# Date filters
# --------------------------------------------------------------------------


def _dated_store(db_path: Path) -> RDNClient:
    client = _local_client(db_path)
    for day, text in ((date(2026, 1, 10), "Heron survey January"), (date(2026, 3, 15), "Heron survey March"), (date(2026, 6, 1), "Heron survey June")):
        saved = client.remember(text)
        # Noon local time keeps the day the same in every time zone.
        _set_saved_at(db_path, saved["address"], datetime(day.year, day.month, day.day, 12).astimezone())
    return client


def test_since_and_until_filter_recall_and_list(tmp_path) -> None:
    client = _dated_store(tmp_path / "rdn.db")

    def contents(results):
        return sorted(r["content"] for r in results)

    assert contents(client.recall("heron", since="2026-03-15")) == ["Heron survey June", "Heron survey March"]
    assert contents(client.recall("heron", until="2026-03-15")) == ["Heron survey January", "Heron survey March"]
    assert contents(client.recall("heron", since="2026-02-01", until="2026-05-31")) == ["Heron survey March"]
    assert contents(client.recall("", since=date(2026, 6, 1))) == ["Heron survey June"]
    assert contents(client.list_prefix("reason://astrognosy", since="2026-03-16")) == ["Heron survey June"]
    assert contents(client.list_prefix("reason://astrognosy", until="2026-01-10")) == ["Heron survey January"]
    assert contents(rdn.list_prefix("reason://astrognosy", since="2026-12-01")) == []


@pytest.mark.parametrize("bad", ["2026-13-01", "2026-02-30", "yesterday", "2026/01/10", "20260110", ""])
def test_bad_dates_are_rejected_clearly(tmp_path, bad) -> None:
    client = _local_client(tmp_path / "rdn.db")
    with pytest.raises(ValueError, match="YYYY-MM-DD|real calendar date"):
        client.recall("x", since=bad)
    with pytest.raises(ValueError):
        client.list_prefix("reason://", until=bad)


def test_since_after_until_is_rejected(tmp_path) -> None:
    client = _local_client(tmp_path / "rdn.db")
    with pytest.raises(ValueError, match="after"):
        rdn.recall("x", since="2026-05-01", until="2026-04-01")
    with pytest.raises(ValueError, match="after"):
        client.list_prefix("reason://", since="2026-05-01", until="2026-04-01")


# --------------------------------------------------------------------------
# Turning node lookup off
# --------------------------------------------------------------------------


@pytest.fixture
def running_node(tmp_path):
    storage = tmp_path / "node"
    storage.mkdir()
    db_path = default_db_path(str(storage))
    server = PrivateWARFNodeServer(("127.0.0.1", 0), PrivateWARFRequestHandler, db_path=db_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", Path(db_path), server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_no_node_setting_ignores_port_file_env_and_config(running_node, monkeypatch, tmp_path) -> None:
    node_url, node_db, port = running_node
    client_mod.PORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    client_mod.PORT_FILE.write_text(str(port), encoding="utf-8")

    # Without the setting, the stray port file redirects writes to the node.
    assert RDNClient().node_url == node_url

    monkeypatch.setenv("REASON_RDN_NO_NODE", "1")
    monkeypatch.setenv("RDN_NODE_URL", node_url)
    client_mod.CONFIG_FILE.write_text(json.dumps({"node_url": node_url}), encoding="utf-8")
    client = RDNClient(node_url=node_url)
    assert client.node_url is None and client.available is False
    assert client.refresh_availability() is False and client.node_url is None
    saved = client.remember("Written straight to the local file")
    assert saved["source"] == "local"
    conn = sqlite3.connect(str(node_db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM warf_artifacts").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("value", ["0", "", "false", "no"])
def test_no_node_setting_needs_an_explicit_yes(value, monkeypatch) -> None:
    monkeypatch.setenv("REASON_RDN_NO_NODE", value)
    assert RDNClient(node_url="http://127.0.0.1:9").node_lookup_disabled is False


# --------------------------------------------------------------------------
# Local web service
# --------------------------------------------------------------------------


def test_node_searches_all_notes_and_supports_forget_update_and_dates(running_node, tmp_path) -> None:
    node_url, node_db, _port = running_node
    client = RDNClient(node_url=node_url, db_path=tmp_path / "mirror.db")
    assert client.available
    saved = client.remember(OLD_NOTE)
    _set_saved_at(node_db, saved["address"], datetime(2025, 1, 5, 12).astimezone())
    _set_saved_at(tmp_path / "mirror.db", saved["address"], datetime(2025, 1, 5, 12).astimezone())
    _fill(client)

    hits = client.recall(QUERY)
    assert hits[0]["content"] == OLD_NOTE and hits[0]["source"] == "node"
    assert client.recall(QUERY, since="2025-02-01") == []
    assert client.recall(QUERY, until="2025-01-05")[0]["content"] == OLD_NOTE

    import requests

    bad = requests.get(f"{node_url}/api/recall", params={"query": "x", "since": "soon"}, timeout=5)
    assert bad.status_code == 400 and "YYYY-MM-DD" in bad.json()["message"]

    updated = client.update(saved["artifact_id"], "Zebra crossing rollout moved to spring")
    assert updated["status"] == "updated" and updated["source"] == "node"
    assert [h["content"] for h in client.recall(QUERY)] == ["Zebra crossing rollout moved to spring"]
    # The mirror copy follows along.
    local_only = _local_client(tmp_path / "mirror.db")
    assert [h["content"] for h in local_only.recall(QUERY)] == ["Zebra crossing rollout moved to spring"]

    forgotten = client.forget(updated["id"])
    assert forgotten["status"] == "forgotten"
    assert client.recall(QUERY) == []
    assert local_only.recall(QUERY) == []

    missing = requests.post(f"{node_url}/api/forget", json={"note": "ffffffffff"}, timeout=5)
    assert missing.status_code == 404


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def _rdn(args, home: Path, extra_env=None):
    env = dict(os.environ)
    env["REASON_RDN_HOME"] = str(home)
    env["REASON_RDN_CONFIG"] = str(home / "none.cfg")
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "rdn.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_cli_old_note_forget_update_and_dates(tmp_path) -> None:
    home = tmp_path / "cli-home"
    old = _local_client(home / "private-node" / "warf-node.db").remember(OLD_NOTE)
    _set_saved_at(home / "private-node" / "warf-node.db", old["address"], datetime(2025, 1, 5, 12).astimezone())
    _fill(_local_client(home / "private-node" / "warf-node.db"))

    recalled = _rdn(["recall", QUERY], home)  # default --limit 10
    assert recalled.returncode == 0, recalled.stderr
    first = json.loads(recalled.stdout)["results"][0]
    assert first["content"] == OLD_NOTE

    assert json.loads(_rdn(["recall", QUERY, "--since", "2025-01-06"], home).stdout)["results"] == []
    bad = _rdn(["recall", QUERY, "--since", "2025-1-6"], home)
    assert bad.returncode == 2 and "YYYY-MM-DD" in bad.stderr
    bad_list = _rdn(["list", "reason://astrognosy", "--until", "tomorrow"], home)
    assert bad_list.returncode == 2 and "YYYY-MM-DD" in bad_list.stderr
    listed = _rdn(["list", "reason://astrognosy", "--until", "2025-01-05"], home)
    assert [r["content"] for r in json.loads(listed.stdout)["results"]] == [OLD_NOTE]

    updated = _rdn(["update", first["id"][:8], "Zebra crossing rollout moved to spring"], home)
    assert updated.returncode == 0, updated.stderr
    new = json.loads(updated.stdout)
    assert new["replaces"] == old["address"]
    contents = [r["content"] for r in json.loads(_rdn(["recall", QUERY], home).stdout)["results"]]
    assert contents == ["Zebra crossing rollout moved to spring"]

    forgot = _rdn(["forget", new["id"]], home)
    assert forgot.returncode == 0 and json.loads(forgot.stdout)["status"] == "forgotten"
    assert json.loads(_rdn(["recall", QUERY], home).stdout)["results"] == []
    hidden = json.loads(_rdn(["recall", QUERY, "--include-hidden"], home).stdout)["results"]
    assert {r["state"] for r in hidden} == {"forgotten", "superseded"}

    purged = _rdn(["forget", old["address"], "--purge"], home)
    assert purged.returncode == 0 and json.loads(purged.stdout)["status"] == "purged"
    missing = _rdn(["forget", old["address"]], home)
    assert missing.returncode == 1 and "No saved note" in missing.stderr


def test_cli_status_explains_shared_layer_and_no_node(tmp_path) -> None:
    home = tmp_path / "cli-home"
    status = _rdn(["status"], home)
    assert status.returncode == 0, status.stderr
    shared = [line for line in status.stdout.splitlines() if line.strip().startswith("Shared")][0]
    assert "not contacted unless you choose it" in shared

    home.mkdir(parents=True, exist_ok=True)
    (home / "private-node").mkdir(exist_ok=True)
    (home / "private-node" / "private-node.port").write_text("9", encoding="utf-8")
    off = _rdn(["status"], home, {"REASON_RDN_NO_NODE": "1"})
    assert off.returncode == 0, off.stderr
    assert "turned off by REASON_RDN_NO_NODE" in off.stdout
    assert "127.0.0.1:9" not in off.stdout


def test_cli_help_mentions_new_commands() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "rdn.cli", "--help"], capture_output=True, text=True, timeout=60
    )
    assert "forget" in result.stdout and "update" in result.stdout
    for command in ("recall", "list", "forget", "update", "status"):
        sub = subprocess.run(
            [sys.executable, "-m", "rdn.cli", command, "--help"], capture_output=True, text=True, timeout=60
        )
        assert sub.returncode == 0
        assert "\u2014" not in sub.stdout  # no em dashes in help text
