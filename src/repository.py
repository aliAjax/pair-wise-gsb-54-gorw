"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE TABLE IF NOT EXISTS notices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    sea_area TEXT NOT NULL,
                    contact_no TEXT NOT NULL,
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    receipt_no TEXT,
                    reject_reason TEXT,
                    revoke_reason TEXT,
                    supersedes_id INTEGER,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_notices_record ON notices(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_notices_state ON notices(state);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    # ---- 航行通告 ----

    @staticmethod
    def _notice_row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def insert_notice(self, record_id: int, sea_area: str, contact_no: str, start_at: str, end_at: str, actor_id: str, supersedes_id: int = None) -> Dict[str, Any]:
        """在立即事务内插入待确认通告，并原子性地强制同海域时段唯一。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            clash = connection.execute(
                "SELECT contact_no FROM notices WHERE state IN ('pending','approved') AND sea_area=? AND start_at < ? AND end_at > ? AND record_id != ? LIMIT 1",
                (sea_area, end_at, start_at, record_id),
            ).fetchone()
            if clash is not None:
                connection.rollback()
                raise Conflict("同一海域时段重叠的通告只能有一份（冲突联系单：%s）" % clash["contact_no"])
            cursor = connection.execute(
                "INSERT INTO notices(record_id,sea_area,contact_no,start_at,end_at,state,supersedes_id,version,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,1,?,?,?,?)",
                (record_id, sea_area, contact_no, start_at, end_at, supersedes_id, actor_id, actor_id, now, now),
            )
            notice_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "register_notice", actor_id, 1, json.dumps({"notice_id": notice_id, "sea_area": sea_area, "contact_no": contact_no, "start_at": start_at, "end_at": end_at, "supersedes_id": supersedes_id}, ensure_ascii=False, sort_keys=True), now),
            )
            row = connection.execute("SELECT * FROM notices WHERE id=?", (notice_id,)).fetchone()
            connection.commit()
        return self._notice_row(row)

    def get_notice(self, notice_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM notices WHERE id=?", (notice_id,)).fetchone()
        if row is None:
            raise NotFound("航行通告不存在")
        return self._notice_row(row)

    def list_notices_for_record(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM notices WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [self._notice_row(row) for row in rows]

    def latest_notices_by_record(self, record_ids: List[int] = None) -> Dict[int, Dict[str, Any]]:
        with self._connect() as connection:
            if record_ids:
                placeholders = ",".join("?" for _ in record_ids)
                sql = "SELECT n.* FROM notices n JOIN (SELECT record_id, MAX(id) AS max_id FROM notices WHERE record_id IN (%s) GROUP BY record_id) m ON n.id = m.max_id" % placeholders
                rows = connection.execute(sql, record_ids).fetchall()
            else:
                rows = connection.execute("SELECT n.* FROM notices n JOIN (SELECT record_id, MAX(id) AS max_id FROM notices GROUP BY record_id) m ON n.id = m.max_id").fetchall()
        return {int(row["record_id"]): self._notice_row(row) for row in rows}

    def mutate_notice(self, notice_id: int, expected_version: int, state: str, fields: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        allowed = {"receipt_no", "reject_reason", "revoke_reason"}
        fields = {key: value for key, value in fields.items() if key in allowed}
        sets = ", ".join("%s=?" % key for key in fields)
        params: List[Any] = list(fields.values())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT record_id, version FROM notices WHERE id=?", (notice_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("航行通告不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            record_id = int(row["record_id"])
            version = int(expected_version) + 1
            if sets:
                connection.execute(
                    "UPDATE notices SET %s, state=?, version=?, updated_by=?, updated_at=? WHERE id=?" % sets,
                    params + [state, version, actor_id, now, notice_id],
                )
            else:
                connection.execute(
                    "UPDATE notices SET state=?, version=?, updated_by=?, updated_at=? WHERE id=?",
                    (state, version, actor_id, now, notice_id),
                )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM notices WHERE id=?", (notice_id,)).fetchone()
            connection.commit()
        return self._notice_row(result)

    def supersede_and_insert(self, notice_id: int, expected_version: int, fields: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        """旧单标记失效并原子性地换发新单（待确认），同时复核海域时段占用。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            old = connection.execute("SELECT * FROM notices WHERE id=?", (notice_id,)).fetchone()
            if old is None:
                connection.rollback()
                raise NotFound("航行通告不存在")
            if int(old["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            record_id = int(old["record_id"])
            clash = connection.execute(
                "SELECT contact_no FROM notices WHERE state IN ('pending','approved') AND sea_area=? AND start_at < ? AND end_at > ? AND record_id != ? LIMIT 1",
                (fields["sea_area"], fields["end_at"], fields["start_at"], record_id),
            ).fetchone()
            if clash is not None:
                connection.rollback()
                raise Conflict("同一海域时段重叠的通告只能有一份（冲突联系单：%s）" % clash["contact_no"])
            connection.execute(
                "UPDATE notices SET state='superseded', version=version+1, updated_by=?, updated_at=? WHERE id=?",
                (actor_id, now, notice_id),
            )
            cursor = connection.execute(
                "INSERT INTO notices(record_id,sea_area,contact_no,start_at,end_at,state,supersedes_id,version,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,1,?,?,?,?)",
                (record_id, fields["sea_area"], fields["contact_no"], fields["start_at"], fields["end_at"], notice_id, actor_id, actor_id, now, now),
            )
            new_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "reschedule_notice", actor_id, 1, json.dumps({"old_notice_id": notice_id, "old_receipt_no": old["receipt_no"], "new_notice_id": new_id, "contact_no": fields["contact_no"], "start_at": fields["start_at"], "end_at": fields["end_at"]}, ensure_ascii=False, sort_keys=True), now),
            )
            row = connection.execute("SELECT * FROM notices WHERE id=?", (new_id,)).fetchone()
            connection.commit()
        return self._notice_row(row)

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
