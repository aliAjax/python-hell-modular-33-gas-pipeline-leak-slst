import json
import sqlite3
from datetime import datetime, timezone

from . import rules
from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, stable_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, source_type, external_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS valve_ledger (
                    valve_id TEXT PRIMARY KEY,
                    owner_item_id INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    released_at TEXT,
                    released_by_item INTEGER,
                    FOREIGN KEY(owner_item_id) REFERENCES items(id)
                );
                """
            )
        finally:
            conn.close()

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload):
        previous = self._last_hash(conn, item_id)
        event = {
            "item_id": item_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
        )

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        initial_status,
                        1,
                        canonical_json(payload),
                        actor,
                        role,
                        now_iso(),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_item", "同一业务实体已经存在")
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key})
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_item(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def list_items(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def add_source(self, item_id, source_type, external_id, payload, observed_at, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            try:
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, source_type, external_id, canonical_json(payload), observed_at, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_source", "同一来源记录已经提交")
            source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn,
                item_id,
                "source_recorded",
                actor,
                role,
                {"source_id": source_id, "source_type": source_type, "external_id": external_id},
            )
            conn.execute("COMMIT")
            return {"id": source_id, "item_id": item_id, "source_type": source_type, "external_id": external_id, "payload": payload, "observed_at": observed_at}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_sources(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM sources WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None, ledger=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            ledger_result = None
            if ledger:
                if ledger["kind"] == "isolate":
                    ledger_result = self._apply_isolation(conn, item_id, ledger["valves"])
                elif ledger["kind"] == "restore":
                    ledger_result = self._apply_restore(conn, item_id, ledger["valves"])
                if ledger_result is not None:
                    event_payload = dict(event_payload)
                    event_payload["ledger"] = ledger_result
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            conn.execute("COMMIT")
            return ledger_result
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _apply_isolation(self, conn, item_id, valves):
        """把阀号挂到在处置事件上；已被其他在处置事件占用的阀门不重复登记，只说明归属。"""
        registered = []
        skipped = []
        for valve in valves:
            row = conn.execute("SELECT * FROM valve_ledger WHERE valve_id=?", (valve,)).fetchone()
            if row is not None and row["state"] == "closed":
                owner = row["owner_item_id"]
                if owner == item_id:
                    registered.append(valve)
                    continue
                owner_row = conn.execute("SELECT status FROM items WHERE id=?", (owner,)).fetchone()
                if owner_row is not None and owner_row["status"] not in rules.TERMINAL_STATUSES:
                    skipped.append({"valve": valve, "owner_item_id": owner})
                    continue
            conn.execute(
                "INSERT OR REPLACE INTO valve_ledger(valve_id,owner_item_id,state,acquired_at,released_at,released_by_item) VALUES(?,?,?,?,?,?)",
                (valve, item_id, "closed", now_iso(), None, None),
            )
            registered.append(valve)
        return {"registered": registered, "skipped": skipped}

    def _apply_restore(self, conn, item_id, valves):
        """只放开没有其他在处置事件占用的阀门；仍被占用的保持关闭并说明占用方。"""
        occupancy = self._valve_occupancy(conn, exclude_item_id=item_id)
        released = []
        held = []
        for valve in valves:
            others = occupancy.get(valve, [])
            if others:
                held.append({"valve": valve, "held_by": others})
                continue
            row = conn.execute("SELECT valve_id FROM valve_ledger WHERE valve_id=?", (valve,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO valve_ledger(valve_id,owner_item_id,state,acquired_at,released_at,released_by_item) VALUES(?,?,?,?,?,?)",
                    (valve, item_id, "open", now_iso(), now_iso(), item_id),
                )
            else:
                conn.execute(
                    "UPDATE valve_ledger SET state='open', released_at=?, released_by_item=? WHERE valve_id=?",
                    (now_iso(), item_id, valve),
                )
            released.append(valve)
        return {"released": released, "held": held}

    def _valve_occupancy(self, conn, exclude_item_id=None):
        """统计每台阀门仍被哪些在处置事件的阀序占用。"""
        rows = conn.execute("SELECT id, status, payload FROM items").fetchall()
        occupancy = {}
        for row in rows:
            if row["status"] in rules.TERMINAL_STATUSES:
                continue
            if exclude_item_id is not None and row["id"] == exclude_item_id:
                continue
            sequence = json.loads(row["payload"]).get("valve_sequence") or []
            for valve in sequence:
                occupancy.setdefault(valve, []).append(row["id"])
        return occupancy

    def valve_status(self, item_id, valves):
        if not valves:
            return []
        conn = self.connect()
        try:
            occupancy = self._valve_occupancy(conn, exclude_item_id=item_id)
            result = []
            for valve in valves:
                row = conn.execute("SELECT * FROM valve_ledger WHERE valve_id=?", (valve,)).fetchone()
                entry = {
                    "valve": valve,
                    "state": row["state"] if row else "unregistered",
                    "owner_item_id": row["owner_item_id"] if row else None,
                }
                others = occupancy.get(valve, [])
                if others:
                    entry["occupied_by"] = others
                result.append(entry)
            return result
        finally:
            conn.close()

    def valve_ledger(self):
        conn = self.connect()
        try:
            occupancy = self._valve_occupancy(conn)
            rows = conn.execute("SELECT * FROM valve_ledger ORDER BY valve_id").fetchall()
            result = []
            for row in rows:
                entry = dict(row)
                entry["occupied_by"] = occupancy.get(row["valve_id"], [])
                result.append(entry)
            return {"valves": result}
        finally:
            conn.close()

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items()}
        finally:
            conn.close()
