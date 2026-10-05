import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


class ValveLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _create(self, pid, seg, region=None):
        return self.service.create_item({
            "pipeline_id": pid,
            "segment_id": seg,
            "reported_at": "2026-09-27T08:00:00+00:00",
            "pressure_drop_kpa": 30,
            "sensor_value_ppm": 120,
            "odor_reports": 3,
            "reporter": "dispatch-1",
        }, "dispatch-1", "dispatcher", region)

    def _verify(self, item, region=None):
        return self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"], region)

    def _isolate(self, item, valves, region=None):
        return self.service.act(item["id"], "isolate", {"valve_sequence": valves}, "sup-1", "supervisor", item["version"], region)

    def _restore(self, item, region=None):
        item = self.service.act(item["id"], "repair", {"work_order": "WO-1"}, "t", "technician", item["version"], region)
        item = self.service.act(item["id"], "pressure_test", {"test_passed": True, "pressure_kpa": 150, "minimum_pressure_kpa": 100}, "t", "technician", item["version"], region)
        return self.service.act(item["id"], "restore", {"hazards_clear": True}, "sup-1", "supervisor", item["version"], region)

    def test_valve_occupied_by_other_event(self):
        a = self._create("P-1", "S-1")
        a = self._verify(a)
        a = self._isolate(a, ["V-1", "V-2"])
        b = self._create("P-2", "S-2")
        b = self._verify(b)
        with self.assertRaises(ConflictError) as context:
            self._isolate(b, ["V-2", "V-3"])
        self.assertEqual(context.exception.code, "valve_occupied")
        self.assertIn("#%s" % a["id"], str(context.exception))
        # 台账仍只登记在 A 名下
        ledger = {row["valve_id"]: row["item_id"] for row in self.repo.list_valve_ledger()}
        self.assertEqual(ledger["V-2"], a["id"])
        self.assertNotIn("V-3", ledger)

    def test_restore_releases_only_own_valves(self):
        a = self._create("P-1", "S-1")
        a = self._verify(a)
        a = self._isolate(a, ["V-1", "V-2"])
        b = self._create("P-2", "S-2")
        b = self._verify(b)
        b = self._isolate(b, ["V-3", "V-4"])
        # A 恢复后只放开自己的 V-1、V-2，B 的 V-3、V-4 仍在用
        a = self._restore(a)
        self.assertEqual(a["status"], "restored")
        ledger = {row["valve_id"]: row["item_id"] for row in self.repo.list_valve_ledger()}
        self.assertNotIn("V-1", ledger)
        self.assertNotIn("V-2", ledger)
        self.assertEqual(ledger["V-3"], b["id"])
        self.assertEqual(ledger["V-4"], b["id"])
        # A 释放后，新事件 C 可以登记 V-2（不会被 A 占用）
        c = self._create("P-3", "S-3")
        c = self._verify(c)
        c = self._isolate(c, ["V-2", "V-5"])
        self.assertEqual(c["payload"]["valve_sequence"], ["V-2", "V-5"])
        ledger = {row["valve_id"]: row["item_id"] for row in self.repo.list_valve_ledger()}
        self.assertEqual(ledger["V-2"], c["id"])
        self.assertEqual(ledger["V-5"], c["id"])

    def test_concurrent_same_event_late_rejected(self):
        item = self._create("P-3", "S-3")
        item = self._verify(item)
        stale_version = item["version"]
        self._isolate(item, ["V-9", "V-10"])
        with self.assertRaises(ConflictError) as context:
            self._isolate(item, ["V-8", "V-7"])
        self.assertEqual(context.exception.code, "version_conflict")

    def test_region_enforced_on_valve_actions(self):
        item = self._create("P-4", "S-4", region="north")
        item = self._verify(item, region="north")
        with self.assertRaises(DomainError) as context:
            self._isolate(item, ["V-5", "V-6"], region="south")
        self.assertEqual(context.exception.code, "region_mismatch")
        # 同辖区可以正常隔离
        item = self._isolate(item, ["V-5", "V-6"], region="north")
        self.assertEqual(item["status"], "isolated")

    def test_duplicate_valve_in_sequence_deduped(self):
        item = self._create("P-5", "S-5")
        item = self._verify(item)
        item = self._isolate(item, ["V-1", "V-1", "V-2"])
        self.assertEqual(item["payload"]["valve_sequence"], ["V-1", "V-2"])


if __name__ == "__main__":
    unittest.main()
