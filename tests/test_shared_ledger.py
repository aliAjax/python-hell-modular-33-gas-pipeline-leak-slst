import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


def make_payload(segment, reported_at, region=None):
    payload = {
        "pipeline_id": "P-9",
        "segment_id": segment,
        "reported_at": reported_at,
        "pressure_drop_kpa": 30,
        "sensor_value_ppm": 120,
        "odor_reports": 2,
        "reporter": "dispatch",
    }
    if region:
        payload["region"] = region
    return payload


class SharedLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _verify_and_isolate(self, segment, reported_at, valves, region=None):
        item = self.service.create_item(make_payload(segment, reported_at, region), "d", "dispatcher", region)
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"], region)
        return self.service.act(item["id"], "isolate", {"valve_sequence": valves}, "s", "supervisor", item["version"], region)

    def _finish_and_restore(self, item, region=None):
        item = self.service.act(item["id"], "repair", {"work_order": "WO-%s" % item["id"]}, "t", "technician", item["version"], region)
        item = self.service.act(item["id"], "pressure_test", {"test_passed": True, "pressure_kpa": 150}, "t", "technician", item["version"], region)
        return self.service.act(item["id"], "restore", {"hazards_clear": True}, "s", "supervisor", item["version"], region)

    def _ledger_states(self):
        return {v["valve_id"]: v["state"] for v in self.service.valve_ledger()["valves"]}

    def test_shared_valve_not_reopened_until_all_incidents_clear(self):
        a = self._verify_and_isolate("S-1", "2026-09-27T08:00:00+00:00", ["V-1", "V-2"])
        self.assertEqual(a["operation"]["ledger"], {"registered": ["V-1", "V-2"], "skipped": []})

        b = self._verify_and_isolate("S-2", "2026-09-27T08:05:00+00:00", ["V-2", "V-3"])
        ledger = b["operation"]["ledger"]
        self.assertEqual(ledger["registered"], ["V-3"])
        self.assertEqual(ledger["skipped"], [{"valve": "V-2", "owner_item_id": a["id"]}])

        a = self._finish_and_restore(a)
        ledger = a["operation"]["ledger"]
        self.assertEqual(ledger["released"], ["V-1"])
        self.assertEqual(ledger["held"], [{"valve": "V-2", "held_by": [b["id"]]}])
        states = self._ledger_states()
        self.assertEqual(states["V-1"], "open")
        self.assertEqual(states["V-2"], "closed")
        self.assertEqual(states["V-3"], "closed")

        b = self._finish_and_restore(b)
        self.assertEqual(sorted(b["operation"]["ledger"]["released"]), ["V-2", "V-3"])
        states = self._ledger_states()
        self.assertEqual(states["V-2"], "open")
        self.assertEqual(states["V-3"], "open")

    def test_valve_reusable_after_owner_finished(self):
        a = self._verify_and_isolate("S-3", "2026-09-27T10:00:00+00:00", ["V-5", "V-6"])
        a = self._finish_and_restore(a)
        self.assertEqual(self._ledger_states()["V-5"], "open")
        c = self._verify_and_isolate("S-4", "2026-09-27T11:00:00+00:00", ["V-5", "V-7"])
        self.assertEqual(c["operation"]["ledger"], {"registered": ["V-5", "V-7"], "skipped": []})
        self.assertEqual(self._ledger_states()["V-5"], "closed")

    def test_concurrent_submit_on_same_item_rejects_late_one(self):
        item = self.service.create_item(make_payload("S-5", "2026-09-27T12:00:00+00:00"), "d", "dispatcher")
        outcomes = []

        def worker():
            try:
                self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"])
                outcomes.append("ok")
            except ConflictError:
                outcomes.append("conflict")

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["conflict", "ok"])
        self.assertEqual(self.service.get_item(item["id"])["version"], item["version"] + 1)

    def test_action_requires_expected_version(self):
        item = self.service.create_item(make_payload("S-6", "2026-09-27T13:00:00+00:00"), "d", "dispatcher")
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder")
        self.assertEqual(context.exception.code, "expected_version_required")

    def test_region_enforcement_on_valve_actions(self):
        item = self.service.create_item(
            make_payload("S-7", "2026-09-27T14:00:00+00:00"), "d", "dispatcher", "east"
        )
        self.assertEqual(item["payload"]["region"], "east")
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"], "east")
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "s", "supervisor", item["version"], "west")
        self.assertEqual(context.exception.code, "region_mismatch")
        self.assertEqual(context.exception.status, 403)
        item = self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "s", "supervisor", item["version"], "east")
        self.assertEqual(item["status"], "isolated")

    def test_regionless_operator_cannot_touch_region_valves(self):
        item = self.service.create_item(
            make_payload("S-8", "2026-09-27T15:00:00+00:00", "east"), "d", "dispatcher"
        )
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"])
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "s", "supervisor", item["version"])
        self.assertEqual(context.exception.code, "region_mismatch")
