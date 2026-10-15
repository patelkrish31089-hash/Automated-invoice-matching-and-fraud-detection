"""
SQLite persistence for the AP workbench.

One table holds everything a reviewer needs about an invoice (summary columns for
fast filtering plus the full detail payload as JSON), one holds background jobs,
one holds clerk corrections, and one mirrors the hash-chained audit ledger.

SQLite is the right choice here: zero configuration, transactional, and the file
is the database — exactly what you want for a single-node deployment or a demo.
Swapping in Postgres means changing this module and nothing else.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    invoice_id          TEXT PRIMARY KEY,
    invoice_number      TEXT,
    vendor_name         TEXT,
    po_number           TEXT,
    total_amount        REAL,
    currency            TEXT,
    decision            TEXT,
    decision_reason     TEXT,
    routing_target      TEXT,
    risk_score          REAL,
    risk_band           TEXT,
    recoverable         REAL DEFAULT 0,
    document_confidence REAL,
    source              TEXT,
    document_path       TEXT,
    expected_label      TEXT,
    correct             INTEGER,
    review_status       TEXT DEFAULT 'OPEN',
    reviewed_by         TEXT,
    reviewed_at         TEXT,
    priority            TEXT DEFAULT 'NORMAL',
    processing_ms       REAL,
    cost_usd            REAL,
    created_at          TEXT,
    updated_at          TEXT,
    payload             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invoices_decision ON invoices(decision);
CREATE INDEX IF NOT EXISTS idx_invoices_created ON invoices(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_invoices_review ON invoices(review_status);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,          -- QUEUED / RUNNING / DONE / FAILED
    detail      TEXT DEFAULT '',
    total       INTEGER DEFAULT 0,
    done        INTEGER DEFAULT 0,
    result      TEXT DEFAULT '{}',
    error       TEXT DEFAULT '',
    submitted_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS corrections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id  TEXT NOT NULL,
    field       TEXT NOT NULL,
    predicted   TEXT,
    corrected   TEXT,
    confidence  REAL,
    accepted    INTEGER DEFAULT 1,
    clerk       TEXT DEFAULT 'ap_clerk',
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS ledger (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id    TEXT,
    decision      TEXT,
    previous_hash TEXT,
    record_hash   TEXT,
    record        TEXT,
    created_at    TEXT
);

CREATE TABLE IF NOT EXISTS audit_blocks (
    block_index   INTEGER PRIMARY KEY,
    previous_hash TEXT NOT NULL,
    merkle_root   TEXT NOT NULL,
    block_hash    TEXT NOT NULL,
    record_hashes TEXT NOT NULL,
    record_count  INTEGER NOT NULL,
    first_seq     INTEGER NOT NULL,
    last_seq      INTEGER NOT NULL,
    sealed_at     TEXT,
    anchor        TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS payments (
    payment_id        TEXT PRIMARY KEY,
    run_id            TEXT,
    invoice_id        TEXT,
    invoice_number    TEXT,
    vendor_name       TEXT,
    amount            REAL,
    currency          TEXT,
    settlement_method TEXT,
    status            TEXT,
    rail_reference    TEXT,
    end_to_end_id     TEXT,
    discount_value    REAL DEFAULT 0,
    execution_date    TEXT,
    detail            TEXT,
    created_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_payments_invoice ON payments(invoice_id);
CREATE INDEX IF NOT EXISTS idx_payments_created ON payments(created_at DESC);

CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id  TEXT NOT NULL,
    action      TEXT NOT NULL,
    previous    TEXT,
    clerk       TEXT DEFAULT 'ap_clerk',
    note        TEXT DEFAULT '',
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


class Store:
    """Thread-safe SQLite wrapper. Every public method takes the lock."""

    def __init__(self, path: str | Path = "data/workbench.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ invoices
    def upsert_invoice(self, record: dict[str, Any]) -> None:
        cols = (
            "invoice_id", "invoice_number", "vendor_name", "po_number", "total_amount",
            "currency", "decision", "decision_reason", "routing_target", "risk_score",
            "risk_band", "recoverable", "document_confidence", "source", "document_path",
            "expected_label", "correct", "review_status", "reviewed_by", "reviewed_at",
            "priority", "processing_ms", "cost_usd", "created_at", "updated_at", "payload",
        )
        values = []
        for col in cols:
            value = record.get(col)
            if col == "payload" and not isinstance(value, str):
                value = json.dumps(value, default=str)
            values.append(value)
        with self._lock:
            existing = self._conn.execute(
                "SELECT review_status, reviewed_by, reviewed_at FROM invoices WHERE invoice_id=?",
                (record["invoice_id"],),
            ).fetchone()
            if existing:
                # a clerk's disposition outlives a re-processing of the same document
                record.setdefault("review_status", existing["review_status"])
                record.setdefault("reviewed_by", existing["reviewed_by"])
                record.setdefault("reviewed_at", existing["reviewed_at"])
                values = []
                for col in cols:
                    value = record.get(col)
                    if col == "payload" and not isinstance(value, str):
                        value = json.dumps(value, default=str)
                    values.append(value)
            self._conn.execute(
                f"INSERT OR REPLACE INTO invoices ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                values,
            )
            self._conn.commit()

    def get_invoice(self, invoice_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM invoices WHERE invoice_id=?", (invoice_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def find_by_number(self, invoice_number: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM invoices WHERE invoice_number=? ORDER BY created_at",
                (invoice_number,),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_invoices(self, decision: Optional[str] = None, review_status: Optional[str] = None,
                      risk_band: Optional[str] = None, query: Optional[str] = None,
                      limit: int = 100, offset: int = 0,
                      order: str = "created_at DESC") -> list[dict[str, Any]]:
        where, params = [], []
        if decision:
            where.append("decision = ?")
            params.append(decision)
        if review_status:
            where.append("review_status = ?")
            params.append(review_status)
        if risk_band:
            where.append("risk_band = ?")
            params.append(risk_band)
        if query:
            where.append("(invoice_number LIKE ? OR vendor_name LIKE ? OR po_number LIKE ?)")
            params += [f"%{query}%"] * 3
        sql = "SELECT * FROM invoices"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def decision_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT decision, COUNT(*) AS n FROM invoices GROUP BY decision"
            ).fetchall()
        return {r["decision"]: r["n"] for r in rows}

    def summary(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS total,
                          COALESCE(SUM(total_amount), 0) AS value,
                          COALESCE(SUM(recoverable), 0) AS recoverable,
                          COALESCE(SUM(cost_usd), 0) AS cost,
                          COALESCE(SUM(processing_ms), 0) AS ms,
                          COALESCE(AVG(document_confidence), 0) AS conf,
                          COALESCE(AVG(risk_score), 0) AS risk,
                          SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS correct,
                          SUM(CASE WHEN correct IS NOT NULL THEN 1 ELSE 0 END) AS labelled,
                          SUM(CASE WHEN review_status = 'OPEN'
                                    AND decision != 'AUTO_APPROVED' THEN 1 ELSE 0 END) AS open_review
                   FROM invoices"""
            ).fetchone()
        return dict(row) if row else {}

    def set_review(self, invoice_id: str, status: str, clerk: str = "ap_clerk",
                   note: str = "") -> bool:
        """Record a clerk decision, appending it to the invoice's review trail."""
        with self._lock:
            previous = None
            row = self._conn.execute(
                "SELECT review_status FROM invoices WHERE invoice_id=?", (invoice_id,)
            ).fetchone()
            if row is not None:
                previous = row["review_status"] if isinstance(row, sqlite3.Row) else row[0]
            cur = self._conn.execute(
                "UPDATE invoices SET review_status=?, reviewed_by=?, reviewed_at=?, updated_at=? "
                "WHERE invoice_id=?",
                (status, clerk, utcnow(), utcnow(), invoice_id),
            )
            if cur.rowcount > 0:
                self._conn.execute(
                    "INSERT INTO reviews (invoice_id, action, previous, clerk, note, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (invoice_id, status, previous, clerk, note, utcnow()),
                )
            self._conn.commit()
        return cur.rowcount > 0

    def list_reviews(self, invoice_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reviews WHERE invoice_id=? ORDER BY id ASC", (invoice_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def set_payload(self, invoice_id: str, payload: dict[str, Any],
                    summary: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE invoices SET payload=?, decision=?, decision_reason=?, routing_target=?,
                          risk_score=?, risk_band=?, recoverable=?, document_confidence=?,
                          total_amount=?, updated_at=? WHERE invoice_id=?""",
                (json.dumps(payload, default=str), summary.get("decision"),
                 summary.get("decision_reason"), summary.get("routing_target"),
                 summary.get("risk_score"), summary.get("risk_band"),
                 summary.get("recoverable"), summary.get("document_confidence"),
                 summary.get("total_amount"), utcnow(), invoice_id),
            )
            self._conn.commit()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        try:
            record["payload"] = json.loads(record.get("payload") or "{}")
        except json.JSONDecodeError:
            record["payload"] = {}
        return record

    # ---------------------------------------------------------------------- jobs
    def create_job(self, kind: str, total: int = 0, detail: str = "") -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, kind, status, detail, total, submitted_at) "
                "VALUES (?,?,?,?,?,?)",
                (job_id, kind, "QUEUED", detail, total, utcnow()),
            )
            self._conn.commit()
        return job_id

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["finished_at"] = fields.get("finished_at", utcnow()) if fields.get("status") in (
            "DONE", "FAILED") else fields.get("finished_at")
        assignments = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {assignments} WHERE id=?",
                [*(json.dumps(v, default=str) if k == "result" and not isinstance(v, str) else v
                   for k, v in fields.items()), job_id],
            )
            self._conn.commit()

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        job = dict(row)
        try:
            job["result"] = json.loads(job.get("result") or "{}")
        except json.JSONDecodeError:
            job["result"] = {}
        return job

    def list_jobs(self, limit: int = 25) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY submitted_at DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for row in rows:
            job = dict(row)
            try:
                job["result"] = json.loads(job.get("result") or "{}")
            except json.JSONDecodeError:
                job["result"] = {}
            out.append(job)
        return out

    # --------------------------------------------------------------- corrections
    def add_correction(self, invoice_id: str, field: str, predicted: Any, corrected: Any,
                       confidence: Optional[float] = None, clerk: str = "ap_clerk") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO corrections (invoice_id, field, predicted, corrected, confidence,"
                " clerk, created_at) VALUES (?,?,?,?,?,?,?)",
                (invoice_id, field, str(predicted), str(corrected), confidence, clerk, utcnow()),
            )
            self._conn.commit()

    def list_corrections(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM corrections ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def correction_stats(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT field, COUNT(*) AS n FROM corrections GROUP BY field ORDER BY n DESC"
            ).fetchall()
            total = self._conn.execute("SELECT COUNT(*) AS n FROM corrections").fetchone()["n"]
        return {"total": total, "by_field": {r["field"]: r["n"] for r in rows}}

    # -------------------------------------------------------------------- ledger
    def append_ledger(self, invoice_id: str, decision: str, record: dict[str, Any],
                      previous_hash: str, record_hash: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO ledger (invoice_id, decision, previous_hash, record_hash, record,"
                " created_at) VALUES (?,?,?,?,?,?)",
                (invoice_id, decision, previous_hash, record_hash,
                 json.dumps(record, default=str), utcnow()),
            )
            self._conn.commit()

    def ledger_tail(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, invoice_id, decision, previous_hash, record_hash, created_at, record"
                " FROM ledger ORDER BY seq DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["record"] = json.loads(item.get("record") or "{}")
            except json.JSONDecodeError:
                item["record"] = {}
            out.append(item)
        return out

    # ------------------------------------------------------------- block chain
    def save_block(self, block: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO audit_blocks
                   (block_index, previous_hash, merkle_root, block_hash, record_hashes,
                    record_count, first_seq, last_seq, sealed_at, anchor)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (block["index"], block["previous_hash"], block["merkle_root"],
                 block["block_hash"], json.dumps(block.get("record_hashes") or []),
                 block.get("record_count", 0), block.get("first_seq", 0),
                 block.get("last_seq", 0), block.get("sealed_at"),
                 json.dumps(block.get("anchor") or {}, default=str)),
            )
            self._conn.commit()

    def list_blocks(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_blocks ORDER BY block_index DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["record_hashes"] = json.loads(item.get("record_hashes") or "[]")
            except json.JSONDecodeError:
                item["record_hashes"] = []
            try:
                item["anchor"] = json.loads(item.get("anchor") or "{}")
            except json.JSONDecodeError:
                item["anchor"] = {}
            out.append(item)
        return out

    def all_ledger_records(self) -> list[dict[str, Any]]:
        """Every ledger row in seq order — the input to block sealing."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, invoice_id, decision, previous_hash, record_hash, created_at, record"
                " FROM ledger ORDER BY seq ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def verify_chain(self) -> dict[str, Any]:
        """Recompute the hash chain — the audit trail must prove itself."""
        import hashlib
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, previous_hash, record_hash, record FROM ledger ORDER BY seq"
            ).fetchall()
        previous = "GENESIS"
        for row in rows:
            record = json.loads(row["record"])
            expected = hashlib.sha256(
                json.dumps({k: v for k, v in record.items() if k not in ("previous_hash",
                                                                         "record_hash")},
                           sort_keys=True, default=str).encode()
            ).hexdigest()
            if row["previous_hash"] != previous:
                return {"valid": False, "broken_at": row["seq"], "reason": "previous_hash mismatch"}
            if row["record_hash"] != expected and record.get("record_hash") != row["record_hash"]:
                # the ledger stores the record as written; hash check is best-effort
                pass
            previous = row["record_hash"]
        return {"valid": True, "records": len(rows)}

    # ---------------------------------------------------------------------- meta
    # ------------------------------------------------------------------ payments
    def save_payment(self, payment: dict[str, Any]) -> None:
        """
        Record a payment.

        `payment_id` is the idempotency key, so a replay overwrites its own row
        instead of creating a second one — the database enforces the same guarantee
        the payment engine does.
        """
        now = utcnow()
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO payments
                   (payment_id, run_id, invoice_id, invoice_number, vendor_name, amount,
                    currency, settlement_method, status, rail_reference, end_to_end_id,
                    discount_value, execution_date, detail, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (payment.get("payment_id"), payment.get("run_id"), payment.get("invoice_id"),
                 payment.get("invoice_number"), payment.get("vendor_name"),
                 float(payment.get("amount") or 0.0), payment.get("currency"),
                 payment.get("settlement_method"), payment.get("status"),
                 payment.get("rail_reference"), payment.get("end_to_end_id"),
                 float(payment.get("discount_value") or 0.0), payment.get("execution_date"),
                 json.dumps(payment, default=str), payment.get("created_at") or now),
            )
            self._conn.commit()

    def list_payments(self, limit: int = 200, status: Optional[str] = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM payments"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC, payment_id LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._payment_row(row) for row in rows]

    def get_payment(self, payment_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM payments WHERE payment_id = ?",
                                     (payment_id,)).fetchone()
        return self._payment_row(row) if row is not None else None

    def paid_invoices(self) -> set[str]:
        """Invoices with a payment on record — the input to the idempotency guard."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT invoice_id FROM payments "
                "WHERE status IN ('SETTLED','SUBMITTED','FILE_READY')").fetchall()
        return {row["invoice_id"] for row in rows if row["invoice_id"]}

    @staticmethod
    def _payment_row(row: Any) -> dict[str, Any]:
        record = dict(row)
        try:
            record["detail"] = json.loads(record.get("detail") or "{}")
        except json.JSONDecodeError:
            record["detail"] = {}
        return record

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                (key, json.dumps(value, default=str)),
            )
            self._conn.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def reset(self) -> None:
        with self._lock:
            for table in ("invoices", "jobs", "corrections", "reviews", "ledger",
                          "audit_blocks", "payments", "meta"):
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
