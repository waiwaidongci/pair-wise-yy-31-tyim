#!/usr/bin/env python3
"""持久化层：SQLite 连接、建表与面向凭证/占用的原子读写。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).with_name("data.db")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def j(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, path: str | Path = DB_PATH):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS dealers (
          id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          country TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS vehicles (
          id INTEGER PRIMARY KEY AUTOINCREMENT, vin TEXT UNIQUE NOT NULL, model TEXT NOT NULL,
          model_year INTEGER NOT NULL, country TEXT NOT NULL, origin_country TEXT NOT NULL, owner_name TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS recalls (
          id INTEGER PRIMARY KEY AUTOINCREMENT, manufacturer TEXT NOT NULL, campaign_code TEXT UNIQUE NOT NULL,
          title TEXT NOT NULL, scope_json TEXT NOT NULL, remedy_version INTEGER NOT NULL,
          remedy_json TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('draft','submitted','published','returned')),
          scope_version INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1,
          review_note TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scope_changes (
          id INTEGER PRIMARY KEY AUTOINCREMENT, recall_id INTEGER NOT NULL REFERENCES recalls(id),
          scope_version INTEGER NOT NULL, scope_json TEXT NOT NULL, created_by TEXT NOT NULL,
          created_at TEXT NOT NULL, UNIQUE(recall_id,scope_version)
        );
        CREATE TABLE IF NOT EXISTS parts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, recall_id INTEGER NOT NULL REFERENCES recalls(id),
          dealer_id INTEGER NOT NULL REFERENCES dealers(id), remedy_version INTEGER NOT NULL,
          available INTEGER NOT NULL CHECK(available>=0), UNIQUE(recall_id,dealer_id,remedy_version)
        );
        CREATE TABLE IF NOT EXISTS repairs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, recall_id INTEGER NOT NULL REFERENCES recalls(id),
          vehicle_id INTEGER NOT NULL REFERENCES vehicles(id), dealer_id INTEGER NOT NULL REFERENCES dealers(id),
          remedy_version INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('reported','confirmed','flagged','withdrawn')),
          evidence_hash TEXT NOT NULL, evidence_consistent INTEGER NOT NULL, cross_border INTEGER NOT NULL DEFAULT 0,
          border_permit TEXT, idempotency_key TEXT NOT NULL, reported_by TEXT NOT NULL,
          reported_at TEXT NOT NULL, reviewed_by TEXT, reviewed_at TEXT, review_note TEXT,
          UNIQUE(recall_id,vehicle_id,idempotency_key)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_confirmed_repair ON repairs(recall_id,vehicle_id) WHERE status='confirmed';
        CREATE TABLE IF NOT EXISTS notifications (
          id INTEGER PRIMARY KEY AUTOINCREMENT, recall_id INTEGER NOT NULL REFERENCES recalls(id),
          vehicle_id INTEGER NOT NULL REFERENCES vehicles(id), scope_version INTEGER NOT NULL,
          channel TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(recall_id,vehicle_id,scope_version)
        );
        CREATE TABLE IF NOT EXISTS regulatory_reports (
          id INTEGER PRIMARY KEY AUTOINCREMENT, recall_id INTEGER NOT NULL REFERENCES recalls(id),
          scope_version INTEGER NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
          created_at TEXT NOT NULL, UNIQUE(recall_id,scope_version)
        );
        CREATE TABLE IF NOT EXISTS permits (
          id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL,
          recall_id INTEGER NOT NULL REFERENCES recalls(id), model TEXT NOT NULL,
          remedy_version INTEGER NOT NULL, country TEXT NOT NULL,
          quota INTEGER NOT NULL CHECK(quota>0),
          occupied INTEGER NOT NULL DEFAULT 0 CHECK(occupied>=0 AND occupied<=quota),
          revoked INTEGER NOT NULL DEFAULT 0, note TEXT,
          issued_by TEXT NOT NULL, issued_at TEXT NOT NULL, expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS permit_occupations (
          id INTEGER PRIMARY KEY AUTOINCREMENT, permit_id INTEGER NOT NULL REFERENCES permits(id),
          repair_id INTEGER REFERENCES repairs(id), request_key TEXT NOT NULL,
          dealer_id INTEGER NOT NULL REFERENCES dealers(id), vin TEXT NOT NULL,
          amount INTEGER NOT NULL DEFAULT 1 CHECK(amount>0),
          status TEXT NOT NULL CHECK(status IN ('held','written_off','released')),
          reason TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(permit_id,request_key)
        );
        CREATE INDEX IF NOT EXISTS idx_occupations_repair ON permit_occupations(repair_id);
        CREATE TABLE IF NOT EXISTS audit_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
          entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL
        );
        """)
        self.conn.commit()

    def audit(self, actor: str, action: str, entity_type: str, entity_id: object, details: dict) -> None:
        self.conn.execute("INSERT INTO audit_log(at,actor,action,entity_type,entity_id,details_json) VALUES(?,?,?,?,?,?)",
                          (now(), actor, action, entity_type, str(entity_id), j(details)))

    # ---- 凭证持久化 ----
    def insert_permit(self, code: str, recall_id: int, model: str, remedy_version: int, country: str,
                      quota: int, expires_at: str, actor: str, note: str) -> int:
        cur = self.conn.execute("""INSERT INTO permits(code,recall_id,model,remedy_version,country,quota,occupied,revoked,note,issued_by,issued_at,expires_at)
                                  VALUES(?,?,?,?,?,?,0,0,?,?,?,?)""",
                                (code, recall_id, model, int(remedy_version), country, int(quota), note, actor, now(), expires_at))
        return int(cur.lastrowid)

    def get_permit(self, identity: object, column: str = "code") -> sqlite3.Row | None:
        return self.conn.execute(f"SELECT * FROM permits WHERE {column}=?", (identity,)).fetchone()

    def list_permits(self, recall_id: int | None = None) -> list[sqlite3.Row]:
        if recall_id is None:
            return list(self.conn.execute("SELECT * FROM permits ORDER BY id"))
        return list(self.conn.execute("SELECT * FROM permits WHERE recall_id=? ORDER BY id", (recall_id,)))

    def guard_occupy(self, permit_id: int, stamp: str, amount: int = 1) -> int:
        """条件加占：仅在未撤销、未过期且余量充足时生效，返回受影响行数。"""
        cur = self.conn.execute("""UPDATE permits SET occupied=occupied+?
                                   WHERE id=? AND revoked=0 AND expires_at>? AND quota-occupied>=?""",
                                (amount, permit_id, stamp, amount))
        return cur.rowcount

    def insert_occupation(self, permit_id: int, request_key: str, dealer_id: int, vin: str, actor: str, amount: int = 1) -> int:
        stamp = now()
        cur = self.conn.execute("""INSERT INTO permit_occupations(permit_id,repair_id,request_key,dealer_id,vin,amount,status,created_by,created_at,updated_at)
                                   VALUES(?,NULL,?,?,?,?, 'held',?,?,?)""",
                                (permit_id, request_key, dealer_id, vin, amount, actor, stamp, stamp))
        return int(cur.lastrowid)

    def find_occupation(self, permit_id: int, request_key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM permit_occupations WHERE permit_id=? AND request_key=?",
                                 (permit_id, request_key)).fetchone()

    def get_occupation(self, occupation_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM permit_occupations WHERE id=?", (occupation_id,)).fetchone()

    def held_occupation_for_repair(self, repair_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM permit_occupations WHERE repair_id=? AND status='held'",
                                 (repair_id,)).fetchone()

    def list_occupations(self, permit_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM permit_occupations WHERE permit_id=? ORDER BY id", (permit_id,)))

    def occupation_counts(self, permit_id: int) -> dict[str, int]:
        counts = {"held": 0, "written_off": 0, "released": 0}
        for row in self.conn.execute("SELECT status, COUNT(*) c FROM permit_occupations WHERE permit_id=? GROUP BY status", (permit_id,)):
            counts[row["status"]] = int(row["c"])
        return counts

    def link_occupation_repair(self, occupation_id: int, repair_id: int) -> None:
        self.conn.execute("UPDATE permit_occupations SET repair_id=?, updated_at=? WHERE id=?",
                          (repair_id, now(), occupation_id))

    def transition_occupation(self, occupation_id: int, status: str, reason: str) -> int:
        cur = self.conn.execute("UPDATE permit_occupations SET status=?, reason=?, updated_at=? WHERE id=? AND status='held'",
                                (status, reason, now(), occupation_id))
        return cur.rowcount

    def adjust_occupied(self, permit_id: int, delta: int) -> None:
        self.conn.execute("UPDATE permits SET occupied=occupied+? WHERE id=? AND occupied+?>=0 AND occupied+?<=quota",
                          (delta, permit_id, delta, delta))

    def close(self) -> None:
        self.conn.close()
