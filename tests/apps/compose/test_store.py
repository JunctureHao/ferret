"""Compose 请求库存储测试：内存副本、文件夹管理、防抖落盘与坏文件回退。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ferret.apps.compose.models import SavedBody, SavedRequest
from ferret.apps.compose.store import ComposeRequestStore
from tests.core.mitm._qt import wait_until

app = QApplication.instance() or QApplication([])


def make_request(
    name: str = "r",
    method: str = "GET",
    url: str = "https://api.example.com/x",
    *,
    folder: str = "",
) -> SavedRequest:
    return SavedRequest.create(name=name, method=method, url=url, folder=folder)


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "compose_requests.json"

    def make_store(self) -> ComposeRequestStore:
        store = ComposeRequestStore(path=self.path)
        self.addCleanup(store.deleteLater)
        return store

    def read_disk(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))


class LoadTests(StoreTestCase):
    def test_missing_file_loads_empty_without_warning(self) -> None:
        store = self.make_store()
        self.assertEqual(store.requests(), [])
        self.assertEqual(store.folders(), [])
        self.assertEqual(store.warning, "")

    def test_corrupt_file_falls_back_to_empty_with_backup(self) -> None:
        self.path.write_text("{ not json !!!", encoding="utf-8")
        store = self.make_store()
        self.assertEqual(store.requests(), [])
        self.assertEqual(store.folders(), [])
        self.assertNotEqual(store.warning, "")
        backups = list(self.path.parent.glob(f"{self.path.name}.corrupt-*"))
        self.assertEqual(len(backups), 1)

    def test_non_object_root_falls_back_to_empty(self) -> None:
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        store = self.make_store()
        self.assertEqual(store.requests(), [])
        self.assertNotEqual(store.warning, "")

    def test_a_single_bad_record_is_skipped(self) -> None:
        good = make_request("good").to_dict()
        bad = {"name": "no id", "method": "GET", "url": "https://x.test/"}
        payload = {"version": 1, "folders": ["F"], "requests": [bad, good]}
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        store = self.make_store()
        requests = store.requests()
        self.assertEqual([r.name for r in requests], ["good"])
        self.assertTrue(store.warning)
        backup = next(self.path.parent.glob(f"{self.path.name}.corrupt-*"))
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), payload)

    def test_duplicate_ids_keep_the_first(self) -> None:
        request = make_request("first")
        other = request.touched(name="second")
        payload = {
            "version": 1,
            "requests": [request.to_dict(), other.to_dict()],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        store = self.make_store()
        self.assertEqual([r.name for r in store.requests()], ["first"])

    def test_folders_are_collected_from_requests_and_folder_list(self) -> None:
        request = make_request("r", folder="A")
        payload = {
            "version": 1,
            "folders": ["A", "B", "", 1, "B"],
            "requests": [request.to_dict()],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        store = self.make_store()
        self.assertEqual(store.folders(), ["A", "B"])


class UpsertRemoveTests(StoreTestCase):
    def test_upsert_appends_and_replaces_by_id(self) -> None:
        store = self.make_store()
        request = make_request("r", folder="F")
        store.upsert(request)
        self.assertEqual(store.requests(), [request])
        # folder 自动登记
        self.assertEqual(store.folders(), ["F"])

        changed_request = request.touched(name="r2", folder="G")
        store.upsert(changed_request)
        self.assertEqual(store.requests(), [changed_request])
        self.assertEqual(store.get(request.id), changed_request)
        self.assertEqual(store.folders(), ["F", "G"])

    def test_get_returns_none_for_unknown_id(self) -> None:
        store = self.make_store()
        self.assertIsNone(store.get("missing"))

    def test_remove_existing_and_missing_id(self) -> None:
        store = self.make_store()
        request = make_request("r")
        store.upsert(request)
        store.remove(request.id)
        self.assertEqual(store.requests(), [])
        # 不存在的 id：无副作用、无落盘义务。
        store._save_pending = False
        store.remove("missing")
        self.assertFalse(store._save_pending)

    def test_changed_signal_fires_on_upsert_and_remove(self) -> None:
        store = self.make_store()
        events: list[str] = []
        store.changed.connect(lambda: events.append("changed"))
        request = make_request("r")
        store.upsert(request)
        store.remove(request.id)
        self.assertEqual(events, ["changed", "changed"])


class FolderTests(StoreTestCase):
    def test_add_folder_deduplicates_and_ignores_blank(self) -> None:
        store = self.make_store()
        store.add_folder("A")
        store.add_folder(" A ")
        store.add_folder("A")
        store.add_folder("   ")
        store.add_folder("")
        self.assertEqual(store.folders(), ["A"])

    def test_rename_folder_updates_requests(self) -> None:
        store = self.make_store()
        store.add_folder("A")
        request = make_request("r", folder="A")
        other = make_request("o", folder="")
        store.upsert(request)
        store.upsert(other)
        store.rename_folder("A", "B")
        self.assertEqual(store.folders(), ["B"])
        self.assertEqual([item.folder for item in store.requests()], ["B", ""])

    def test_rename_folder_noops_on_bad_args(self) -> None:
        store = self.make_store()
        store.add_folder("A")
        store.add_folder("B")
        for old, new in (
            ("A", "A"),
            ("A", "B"),
            ("A", "  "),
            ("missing", "C"),
            ("", "C"),
        ):
            with self.subTest(old=old, new=new):
                store.rename_folder(old, new)
        self.assertEqual(store.folders(), ["A", "B"])

    def test_remove_folder_drops_requests_to_ungrouped(self) -> None:
        store = self.make_store()
        store.add_folder("A")
        request = make_request("r", folder="A")
        keep = make_request("k", folder="")
        store.upsert(request)
        store.upsert(keep)
        store.remove_folder("A")
        self.assertEqual(store.folders(), [])
        self.assertEqual([item.folder for item in store.requests()], ["", ""])

    def test_remove_unknown_folder_noops(self) -> None:
        store = self.make_store()
        store._save_pending = False
        store.remove_folder("missing")
        self.assertFalse(store._save_pending)


class PersistenceTests(StoreTestCase):
    def test_flush_writes_and_a_new_store_reads_it_back(self) -> None:
        store = self.make_store()
        store.add_folder("F")
        store.upsert(
            make_request("下单", "POST", "https://api.example.com/orders", folder="F")
        )
        store.upsert(
            SavedRequest.create(
                name="raw",
                method="PUT",
                url="https://api.example.com/blob",
                body=SavedBody(kind="raw", raw_b64="aGk="),
            )
        )
        store.flush()
        self.assertFalse(store._save_pending)

        disk = self.read_disk()
        self.assertEqual(disk["version"], 1)

        reloaded = self.make_store()
        self.assertEqual(reloaded.requests(), store.requests())
        self.assertEqual(reloaded.folders(), store.folders())
        self.assertEqual(reloaded.warning, "")

    def test_save_is_debounced_until_flush(self) -> None:
        store = self.make_store()
        store.upsert(make_request("r"))
        # 防抖定时器还没触发，写盘义务挂着。
        self.assertTrue(store._save_pending)
        store.flush()
        self.assertFalse(store._save_pending)
        self.assertTrue(self.path.exists())
        self.assertEqual(len(self.read_disk()["requests"]), 1)

    def test_debounced_timer_eventually_writes(self) -> None:
        store = self.make_store()
        store.upsert(make_request("r"))
        # 超过 300ms 防抖窗口后轮询定时器落盘结果。
        self.assertTrue(wait_until(lambda: not store._save_pending, timeout_ms=5000))
        self.assertFalse(store._save_pending)
        self.assertTrue(self.path.exists())

    def test_write_failure_retains_disk_and_can_be_retried(self) -> None:
        store = self.make_store()
        request = make_request("original")
        store.upsert(request)
        self.assertTrue(store.flush())
        original = self.path.read_bytes()
        failures = []
        store.save_failed.connect(failures.append)
        store.upsert(request.touched(name="changed"))
        with patch.object(Path, "replace", side_effect=PermissionError("locked")):
            self.assertFalse(store.flush())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(store._save_pending)
        self.assertTrue(failures)
        self.assertEqual(list(self.path.parent.glob(f".{self.path.name}.*")), [])
        self.assertTrue(store.flush())
        self.assertEqual(self.make_store().requests()[0].name, "changed")

    def test_headers_with_surrogateescaped_bytes_round_trip(self) -> None:
        value = b"caf\xe9\xff".decode("utf-8", "surrogateescape")
        request = make_request().touched(headers=(("X-Bytes", value),))
        store = self.make_store()
        store.upsert(request)
        self.assertTrue(store.flush())
        self.assertEqual(self.make_store().requests()[0], request)

    def test_future_schema_is_never_overwritten(self) -> None:
        original = '{"version": 999, "requests": [], "future": "keep"}'
        self.path.write_text(original, encoding="utf-8")
        store = self.make_store()
        store.upsert(make_request())
        self.assertFalse(store.flush())
        self.assertTrue(store.warning)
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_malformed_collections_do_not_crash_or_overwrite_without_backup(
        self,
    ) -> None:
        for raw in ({"requests": 4}, {"requests": {}}, {"folders": "wrong"}):
            with self.subTest(raw=raw):
                original = json.dumps(raw)
                self.path.write_text(original, encoding="utf-8")
                with patch(
                    "ferret.apps.compose.store.shutil.copy2",
                    side_effect=OSError("denied"),
                ):
                    store = self.make_store()
                store.upsert(make_request())
                self.assertFalse(store.flush())
                self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_unreadable_library_is_never_replaced_by_empty_data(self) -> None:
        self.path.write_text('{"requests": []}', encoding="utf-8")
        with patch(
            "ferret.apps.compose.store.Config._read",
            side_effect=PermissionError("locked"),
        ):
            store = self.make_store()
        store.upsert(make_request())
        self.assertFalse(store.flush())
        self.assertEqual(self.path.read_text(encoding="utf-8"), '{"requests": []}')


if __name__ == "__main__":
    unittest.main()
