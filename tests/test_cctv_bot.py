import os
import json
import pytest
from unittest.mock import MagicMock, patch
import cctv_bot


class DummyZotero:
    def __init__(self, version=100, collections=None, top_items=None, deleted=None, modified=None):
        self._version = version
        self._collections = collections or []
        self._top_items = top_items or []
        self._deleted = deleted or {"items": []}
        self._modified = modified or []

    def last_modified_version(self):
        return self._version

    def collections(self):
        return self._collections

    def top(self):
        return self._top_items

    def items(self, since=0):
        return self._modified

    def deleted(self, since=0):
        return self._deleted

    def everything(self, result):
        return result


def test_is_trackable_top_item():
    # Regular top-level journal article
    assert cctv_bot.is_trackable_top_item({"itemType": "journalArticle", "title": "Paper A"}) is True
    # Child item with parentItem
    assert cctv_bot.is_trackable_top_item({"itemType": "journalArticle", "parentItem": "PARENT1"}) is False
    # Standalone attachment, note, annotation
    assert cctv_bot.is_trackable_top_item({"itemType": "attachment", "title": "PDF"}) is False
    assert cctv_bot.is_trackable_top_item({"itemType": "note", "note": "Memo"}) is False
    assert cctv_bot.is_trackable_top_item({"itemType": "annotation"}) is False


def test_heal_unknown_collections():
    state = {
        "ITEM1": {"title": "Study 1", "collections": ["알수없는폴더(COL105)", "01_Rehab"]},
        "ITEM2": {"title": "Study 2", "collections": ["알수없는폴더(DELETED99)"]},
    }
    col_map = {"COL105": "20_Prosthetics"}
    healed = cctv_bot.heal_unknown_collections(state, col_map)
    assert healed == 1
    assert state["ITEM1"]["collections"] == ["01_Rehab", "20_Prosthetics"]
    assert state["ITEM2"]["collections"] == ["알수없는폴더(DELETED99)"]


def test_initial_scan_and_fast_skip(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(cctv_bot, "STATE_FILE", str(state_file))

    collections = [{"key": "C1", "data": {"name": "Stroke"}}, {"key": "C2", "data": {"name": "Robotics"}}]
    top_items = [
        {"key": "K1", "data": {"itemType": "journalArticle", "title": "Paper 1", "collections": ["C1"]}},
        {"key": "K2", "data": {"itemType": "note", "note": "Standalone note", "collections": ["C1"]}},
        {"key": "K3", "data": {"itemType": "journalArticle", "title": "Trashed Paper", "deleted": 1, "collections": ["C2"]}},
    ]

    zot = DummyZotero(version=50, collections=collections, top_items=top_items)
    cctv_bot.run_audit(zot)

    assert state_file.exists()
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["library_version"] == 50
    assert "K1" in saved["items"]
    assert saved["items"]["K1"]["collections"] == ["Stroke"]
    # Standalone note and deleted item must be excluded
    assert "K2" not in saved["items"]
    assert "K3" not in saved["items"]

    # Second run with identical version should fast-skip without calling collections()
    zot.collections = MagicMock(side_effect=AssertionError("Should not be called on fast-skip!"))
    cctv_bot.run_audit(zot)


def test_incremental_scan_trash_and_delete_and_move(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(cctv_bot, "STATE_FILE", str(state_file))
    monkeypatch.setattr(cctv_bot, "ALERT_ON_DELETE", True)

    initial_data = {
        "library_version": 50,
        "collections_map": {"C1": "Folder A", "C2": "Folder B"},
        "items": {
            "KEEP_MOVE": {"title": "Moving Paper", "collections": ["Folder A"]},
            "TO_TRASH": {"title": "Trashed Paper", "collections": ["Folder A"]},
            "TO_PURGE": {"title": "Purged Paper", "collections": ["Folder B"]},
        },
    }
    state_file.write_text(json.dumps(initial_data), encoding="utf-8")

    collections = [{"key": "C1", "data": {"name": "Folder A"}}, {"key": "C2", "data": {"name": "Folder B"}}]
    deleted = {"items": ["TO_PURGE"]}
    modified = [
        # Trashed item (deleted: 1 in modified items)
        {"key": "TO_TRASH", "data": {"itemType": "journalArticle", "title": "Trashed Paper", "deleted": 1, "collections": ["C1"]}},
        # Moved item (C1 -> C2)
        {"key": "KEEP_MOVE", "data": {"itemType": "journalArticle", "title": "Moving Paper", "collections": ["C2"]}},
        # Newly added item
        {"key": "NEW_ITEM", "data": {"itemType": "journalArticle", "title": "Brand New Paper", "collections": ["C1"]}},
    ]

    zot = DummyZotero(version=55, collections=collections, deleted=deleted, modified=modified)

    alerts_sent = []
    monkeypatch.setattr(cctv_bot, "send_telegram_alert", lambda msg, dry_run=False: alerts_sent.append(msg))

    cctv_bot.run_audit(zot)

    # Both TO_PURGE and TO_TRASH should trigger deletion alerts
    assert len(alerts_sent) == 2
    assert any("Purged Paper" in a for a in alerts_sent)
    assert any("Trashed Paper" in a for a in alerts_sent)

    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["library_version"] == 55
    assert "TO_TRASH" not in saved["items"]
    assert "TO_PURGE" not in saved["items"]
    assert saved["items"]["KEEP_MOVE"]["collections"] == ["Folder B"]
    assert saved["items"]["NEW_ITEM"]["title"] == "Brand New Paper"


def test_retry_api_call():
    attempts = {"count": 0}

    def flaky_fn():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ConnectionError("Temporary DNS error")
        return "OK"

    res = cctv_bot.retry_api_call(flaky_fn, max_retries=3, base_delay=0.01)
    assert res == "OK"
    assert attempts["count"] == 3
