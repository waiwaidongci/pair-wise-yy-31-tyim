#!/usr/bin/env python3
"""HTTP 入口：召回、跨境维修与许可凭证的路由分发。

业务分层：
- store.py            持久化（SQLite 表结构与事务）
- permit_service.py   许可判定（签发、占用、核销、释放、余额）
- app.py              入口（HTTP 处理 + 召回/维修流程编排）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from permit_service import PermitService
from store import DB_PATH, ApiError, Store, j, now


class RecallService:
    def __init__(self, store: Store, permits: PermitService | None = None):
        self.store, self.conn = store, store.conn
        self.permits = permits or PermitService(store)

    @staticmethod
    def _actor(actor: str | None, role: str | None, allowed: set[str]) -> str:
        if not actor: raise ApiError(401, "缺少身份")
        if role not in allowed: raise ApiError(403, "角色无权执行此操作")
        return actor

    def _row(self, table: str, identity: object, column: str = "id") -> sqlite3.Row:
        row = self.conn.execute(f"SELECT * FROM {table} WHERE {column}=?", (identity,)).fetchone()
        if not row: raise ApiError(404, "对象不存在")
        return row

    def register_dealer(self, actor: str | None, role: str | None, code: str, name: str, country: str) -> dict:
        actor = self._actor(actor, role, {"regulator"})
        if not code or not country: raise ApiError(400, "维修网点代号和国家不能为空")
        try:
            with self.conn:
                cur = self.conn.execute("INSERT INTO dealers(code,name,country) VALUES(?,?,?)", (code, name, country))
                self.store.audit(actor, "dealer.register", "dealer", cur.lastrowid, {"code": code, "country": country})
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "维修网点代号已存在") from exc
        return {"id": cur.lastrowid, "code": code, "name": name, "country": country, "active": True}

    def register_vehicle(self, actor: str | None, role: str | None, vin: str, model: str, model_year: int, country: str, owner_name: str) -> dict:
        actor = self._actor(actor, role, {"manufacturer", "regulator"})
        vin = vin.upper().strip()
        if len(vin) < 5 or not model or int(model_year) < 1900: raise ApiError(400, "车辆识别信息不完整")
        try:
            with self.conn:
                cur = self.conn.execute("INSERT INTO vehicles(vin,model,model_year,country,origin_country,owner_name,updated_at) VALUES(?,?,?,?,?,?,?)",
                                        (vin, model, int(model_year), country, country, owner_name, now()))
                self.store.audit(actor, "vehicle.register", "vehicle", cur.lastrowid, {"vin": vin, "country": country})
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "车辆识别码已存在") from exc
        return {"id": cur.lastrowid, "vin": vin, "model": model, "model_year": model_year, "country": country, "owner_name": owner_name}

    def transfer_vehicle(self, actor: str | None, role: str | None, vin: str, country: str, owner_name: str) -> dict:
        actor = self._actor(actor, role, {"dealer", "regulator"})
        vehicle = self._row("vehicles", vin.upper(), "vin")
        with self.conn:
            self.conn.execute("UPDATE vehicles SET country=?,owner_name=?,updated_at=? WHERE id=?", (country, owner_name, now(), vehicle["id"]))
            self.store.audit(actor, "vehicle.transfer", "vehicle", vehicle["id"], {"old_country": vehicle["country"], "new_country": country, "owner_name": owner_name})
        return dict(self._row("vehicles", vehicle["id"]))

    def create_recall(self, actor: str | None, role: str | None, campaign_code: str, title: str, scope: dict, remedy: dict) -> dict:
        actor = self._actor(actor, role, {"manufacturer"})
        self._validate_scope(scope)
        if not campaign_code.strip() or not remedy.get("description") or not remedy.get("version"):
            raise ApiError(400, "召回活动编号和修复方案不能为空")
        stamp = now()
        try:
            with self.conn:
                cur = self.conn.execute("""INSERT INTO recalls(manufacturer,campaign_code,title,scope_json,remedy_version,remedy_json,state,created_by,created_at,updated_at)
                                         VALUES(?,?,?,?,?,?, 'draft',?,?,?)""",
                                        (actor, campaign_code, title, j(scope), int(remedy["version"]), j(remedy), actor, stamp, stamp))
                self.store.audit(actor, "recall.create", "recall", cur.lastrowid, {"campaign_code": campaign_code, "remedy_version": remedy["version"]})
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "召回活动编号已存在") from exc
        return self._recall_dict(self._row("recalls", cur.lastrowid))

    def submit_recall(self, actor: str | None, role: str | None, recall_id: int, expected_version: int) -> dict:
        actor = self._actor(actor, role, {"manufacturer"})
        recall = self._row("recalls", recall_id)
        if recall["created_by"] != actor: raise ApiError(403, "只能提交本机构创建的召回")
        if recall["state"] != "draft": raise ApiError(409, "只有草稿可以提交")
        return self._recall_state_change(recall, "submitted", expected_version, actor, "提交监管审核")

    def review_recall(self, actor: str | None, role: str | None, recall_id: int, decision: str, expected_version: int, note: str = "") -> dict:
        actor = self._actor(actor, role, {"regulator"})
        if decision not in {"publish", "return"}: raise ApiError(400, "决定只能是 publish 或 return")
        recall = self._row("recalls", recall_id)
        if recall["state"] != "submitted": raise ApiError(409, "只有已提交召回可以审核")
        if int(expected_version) != int(recall["revision"]): raise ApiError(409, "召回已被修改，请刷新版本")
        state = "published" if decision == "publish" else "returned"
        result = self._recall_state_change(recall, state, expected_version, actor, note)
        if state == "published":
            self._create_release_artifacts(recall["id"], int(recall["scope_version"]), actor)
            result = self._recall_dict(self._row("recalls", recall_id))
        return result

    def _recall_state_change(self, recall: sqlite3.Row, state: str, expected_version: int, actor: str, note: str) -> dict:
        if int(expected_version) != int(recall["revision"]): raise ApiError(409, "召回版本冲突")
        with self.conn:
            cur = self.conn.execute("UPDATE recalls SET state=?,revision=revision+1,review_note=?,updated_at=? WHERE id=? AND revision=?",
                                    (state, note, now(), recall["id"], expected_version))
            if cur.rowcount != 1: raise ApiError(409, "并发更新冲突")
            self.store.audit(actor, f"recall.{state}", "recall", recall["id"], {"note": note, "scope_version": recall["scope_version"]})
        return self._recall_dict(self._row("recalls", recall["id"]))

    def change_scope(self, actor: str | None, role: str | None, recall_id: int, scope: dict, expected_version: int) -> dict:
        actor = self._actor(actor, role, {"manufacturer"})
        self._validate_scope(scope)
        recall = self._row("recalls", recall_id)
        if recall["manufacturer"] != actor: raise ApiError(403, "只能调整本机构的召回范围")
        if recall["state"] != "published": raise ApiError(409, "只有已发布召回可以调整范围")
        if int(expected_version) != int(recall["revision"]): raise ApiError(409, "召回已被修改，请刷新版本")
        scope_version = int(recall["scope_version"]) + 1
        with self.conn:
            self.conn.execute("UPDATE recalls SET scope_json=?,scope_version=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                              (j(scope), scope_version, now(), recall_id, expected_version))
            self.conn.execute("INSERT INTO scope_changes(recall_id,scope_version,scope_json,created_by,created_at) VALUES(?,?,?,?,?)",
                              (recall_id, scope_version, j(scope), actor, now()))
            self.store.audit(actor, "recall.scope_change", "recall", recall_id, {"scope_version": scope_version, "scope": scope})
        self._create_release_artifacts(recall_id, scope_version, actor)
        return self._recall_dict(self._row("recalls", recall_id))

    def revise_remedy(self, actor: str | None, role: str | None, recall_id: int, remedy: dict, expected_version: int) -> dict:
        """修复方案换版：版本号递增，旧版许可在占用时将被拒绝。"""
        actor = self._actor(actor, role, {"manufacturer"})
        recall = self._row("recalls", recall_id)
        if recall["manufacturer"] != actor: raise ApiError(403, "只能修订本机构召回的修复方案")
        if recall["state"] != "published": raise ApiError(409, "只有已发布召回可以换版修复方案")
        if int(expected_version) != int(recall["revision"]): raise ApiError(409, "召回已被修改，请刷新版本")
        if not remedy.get("description"): raise ApiError(400, "修复方案描述不能为空")
        new_version = int(recall["remedy_version"]) + 1
        remedy = {**{k: v for k, v in remedy.items() if k not in {"version", "description"}},
                  "version": new_version, "description": str(remedy["description"])}
        with self.conn:
            cur = self.conn.execute("UPDATE recalls SET remedy_version=?,remedy_json=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                                    (new_version, j(remedy), now(), recall_id, expected_version))
            if cur.rowcount != 1: raise ApiError(409, "并发更新冲突")
            self.store.audit(actor, "recall.remedy_revise", "recall", recall_id, {"remedy_version": new_version})
        return self._recall_dict(self._row("recalls", recall_id))

    def add_parts(self, actor: str | None, role: str | None, recall_id: int, dealer_id: int, remedy_version: int, quantity: int) -> dict:
        actor = self._actor(actor, role, {"manufacturer", "regulator"})
        if quantity <= 0: raise ApiError(400, "入库数量必须大于零")
        recall = self._row("recalls", recall_id); dealer = self._row("dealers", dealer_id)
        if recall["state"] not in {"published", "submitted"}: raise ApiError(409, "召回尚未进入可备件状态")
        with self.conn:
            self.conn.execute("""INSERT INTO parts(recall_id,dealer_id,remedy_version,available) VALUES(?,?,?,?)
                               ON CONFLICT(recall_id,dealer_id,remedy_version) DO UPDATE SET available=available+excluded.available""",
                              (recall_id, dealer_id, remedy_version, quantity))
            self.store.audit(actor, "parts.add", "recall", recall_id, {"dealer_id": dealer_id, "quantity": quantity, "remedy_version": remedy_version})
        row = self.conn.execute("SELECT * FROM parts WHERE recall_id=? AND dealer_id=? AND remedy_version=?", (recall_id, dealer_id, remedy_version)).fetchone()
        return dict(row)

    def report_repair(self, actor: str | None, role: str | None, recall_id: int, vin: str, dealer_id: int, remedy_version: int, evidence_hash: str, evidence_consistent: bool, border_permit: str = "", idempotency_key: str = "") -> dict:
        actor = self._actor(actor, role, {"dealer"})
        if not idempotency_key or not evidence_hash: raise ApiError(400, "证据哈希和幂等键不能为空")
        recall = self._row("recalls", recall_id)
        if recall["state"] != "published": raise ApiError(409, "召回尚未发布")
        if int(remedy_version) != int(recall["remedy_version"]): raise ApiError(409, "维修方案版本不是当前版本")
        vehicle = self._row("vehicles", vin.upper(), "vin")
        dealer = self._row("dealers", dealer_id)
        if not dealer["active"]: raise ApiError(409, "维修网点已停用")
        existing = self.conn.execute("SELECT * FROM repairs WHERE recall_id=? AND vehicle_id=? AND idempotency_key=?", (recall_id, vehicle["id"], idempotency_key)).fetchone()
        if existing: return dict(existing)
        duplicate = self.conn.execute("SELECT id FROM repairs WHERE recall_id=? AND vehicle_id=? AND status IN ('reported','confirmed')", (recall_id, vehicle["id"])).fetchone()
        if duplicate: raise ApiError(409, "该车辆已有维修记录")
        scope = json.loads(recall["scope_json"])
        if not self._in_scope(vehicle, scope): raise ApiError(409, "车辆不在当前召回范围内")
        # 跨境：车辆在原籍国（登记来源国）以外的网点维修
        cross_border = dealer["country"] != vehicle["origin_country"]
        part = self.conn.execute("SELECT * FROM parts WHERE recall_id=? AND dealer_id=? AND remedy_version=?", (recall_id, dealer_id, remedy_version)).fetchone()
        if not part or int(part["available"]) < 1: raise ApiError(409, "维修网点零件库存不足")
        with self.store.transaction():
            # 跨境：同一许可并发占用只放行一笔；过期/国家不符/换版/余量不足均拒绝。
            usage_id = None
            if cross_border:
                usage_id = self.permits._occupy_locked(border_permit, idempotency_key, vehicle["vin"], dealer_id, actor)
            cur = self.conn.execute("""INSERT INTO repairs(recall_id,vehicle_id,dealer_id,remedy_version,status,evidence_hash,evidence_consistent,
                                     cross_border,border_permit,permit_usage_id,idempotency_key,reported_by,reported_at)
                                     VALUES(?,?,?,?, 'reported',?,?,?,?,?,?,?,?)""",
                                    (recall_id, vehicle["id"], dealer_id, remedy_version, evidence_hash, int(evidence_consistent),
                                     int(cross_border), border_permit if cross_border else None, usage_id, idempotency_key, actor, now()))
            stock = self.conn.execute("UPDATE parts SET available=available-1 WHERE id=? AND available>0", (part["id"],))
            if stock.rowcount != 1: raise ApiError(409, "维修网点零件库存不足")
            if usage_id:
                self.conn.execute("UPDATE permit_usages SET repair_id=? WHERE id=?", (cur.lastrowid, usage_id))
            self.store.audit(actor, "repair.report", "repair", cur.lastrowid, {"recall_id": recall_id, "vin": vehicle["vin"], "cross_border": cross_border, "permit_usage_id": usage_id})
        return dict(self._row("repairs", cur.lastrowid))

    def review_repair(self, actor: str | None, role: str | None, repair_id: int, decision: str, note: str = "") -> dict:
        actor = self._actor(actor, role, {"regulator"})
        if decision not in {"confirm", "flag"}: raise ApiError(400, "决定只能是 confirm 或 flag")
        repair = self._row("repairs", repair_id)
        if repair["status"] != "reported": raise ApiError(409, "维修记录已经复核")
        new_status = "confirmed" if decision == "confirm" and repair["evidence_consistent"] else "flagged"
        with self.store.transaction():
            self.conn.execute("UPDATE repairs SET status=?,reviewed_by=?,reviewed_at=?,review_note=? WHERE id=?", (new_status, actor, now(), note, repair_id))
            if new_status == "flagged":
                self.conn.execute("UPDATE parts SET available=available+1 WHERE recall_id=? AND dealer_id=? AND remedy_version=?",
                                  (repair["recall_id"], repair["dealer_id"], repair["remedy_version"]))
            # 复核通过 -> 核销额度；复核打回 -> 退回占用。使用记录均保留。
            if repair["permit_usage_id"]:
                usage = self._row("permit_usages", repair["permit_usage_id"])
                if new_status == "confirmed":
                    self.permits._settle_locked(usage, "written_off", "confirm", actor, note)
                else:
                    self.permits._settle_locked(usage, "released", "return", actor, note)
            self.store.audit(actor, "repair.review", "repair", repair_id, {"decision": decision, "status": new_status, "note": note})
        return dict(self._row("repairs", repair_id))

    def withdraw_repair(self, actor: str | None, role: str | None, repair_id: int, note: str = "") -> dict:
        actor = self._actor(actor, role, {"dealer", "regulator"})
        repair = self._row("repairs", repair_id)
        if role == "dealer" and repair["reported_by"] != actor: raise ApiError(403, "只能撤回本网点上报的维修")
        if repair["status"] != "reported": raise ApiError(409, "只有待复核的维修可以撤回")
        with self.store.transaction():
            self.conn.execute("UPDATE repairs SET status='withdrawn',reviewed_by=?,reviewed_at=?,review_note=? WHERE id=?",
                              (actor, now(), note or "网点撤回", repair_id))
            self.conn.execute("UPDATE parts SET available=available+1 WHERE recall_id=? AND dealer_id=? AND remedy_version=?",
                              (repair["recall_id"], repair["dealer_id"], repair["remedy_version"]))
            if repair["permit_usage_id"]:
                usage = self._row("permit_usages", repair["permit_usage_id"])
                self.permits._settle_locked(usage, "released", "withdraw", actor, note)
            self.store.audit(actor, "repair.withdraw", "repair", repair_id, {"note": note})
        return dict(self._row("repairs", repair_id))

    def _create_release_artifacts(self, recall_id: int, scope_version: int, actor: str) -> None:
        recall = self._row("recalls", recall_id); scope = json.loads(recall["scope_json"])
        vehicles = [row for row in self.conn.execute("SELECT * FROM vehicles ORDER BY id") if self._in_scope(row, scope)]
        with self.conn:
            for vehicle in vehicles:
                self.conn.execute("""INSERT OR IGNORE INTO notifications(recall_id,vehicle_id,scope_version,channel,status,created_at)
                                     VALUES(?,?,?, 'owner-notice','queued',?)""", (recall_id, vehicle["id"], scope_version, now()))
            payload = {"campaign_code": recall["campaign_code"], "scope_version": scope_version, "scope": scope,
                       "remedy_version": recall["remedy_version"], "affected_count": len(vehicles)}
            self.conn.execute("INSERT OR IGNORE INTO regulatory_reports(recall_id,scope_version,payload_json,status,created_at) VALUES(?,?,?, 'queued',?)",
                              (recall_id, scope_version, j(payload), now()))
            self.store.audit(actor, "recall.artifacts", "recall", recall_id, {"scope_version": scope_version, "affected_count": len(vehicles)})

    def unfinished(self, actor: str | None, role: str | None, recall_id: int) -> dict:
        self._actor(actor, role, {"manufacturer", "regulator"})
        recall = self._row("recalls", recall_id)
        scope = json.loads(recall["scope_json"])
        confirmed = {row["vehicle_id"] for row in self.conn.execute("SELECT vehicle_id FROM repairs WHERE recall_id=? AND status='confirmed'", (recall_id,))}
        current_year = datetime.now(timezone.utc).year
        items = []
        for vehicle in self.conn.execute("SELECT * FROM vehicles ORDER BY vin"):
            if self._in_scope(vehicle, scope) and vehicle["id"] not in confirmed:
                items.append({"vin": vehicle["vin"], "model": vehicle["model"], "model_year": vehicle["model_year"],
                              "country": vehicle["country"], "risk": "high" if current_year - int(vehicle["model_year"]) >= 8 else "normal"})
        return {"recall_id": recall_id, "scope_version": recall["scope_version"], "unfinished_count": len(items), "vehicles": items}

    @staticmethod
    def _in_scope(vehicle: sqlite3.Row, scope: dict) -> bool:
        return (vehicle["model"] in scope.get("models", []) and int(vehicle["model_year"]) in scope.get("model_years", [])
                and any(vehicle["vin"].startswith(prefix.upper()) for prefix in scope.get("vin_prefixes", []))
                and (vehicle["country"] in scope.get("countries", []) or vehicle["origin_country"] in scope.get("countries", [])))

    @staticmethod
    def _validate_scope(scope: dict) -> None:
        for key in ("models", "model_years", "vin_prefixes", "countries"):
            if not scope.get(key): raise ApiError(400, f"召回范围缺少 {key}")

    def _recall_dict(self, row: sqlite3.Row) -> dict:
        return {"id": row["id"], "manufacturer": row["manufacturer"], "campaign_code": row["campaign_code"], "title": row["title"],
                "scope": json.loads(row["scope_json"]), "scope_version": row["scope_version"], "remedy": json.loads(row["remedy_json"]),
                "remedy_version": row["remedy_version"], "state": row["state"], "revision": row["revision"], "review_note": row["review_note"]}

    def recall_detail(self, recall_id: int) -> dict:
        result = self._recall_dict(self._row("recalls", recall_id))
        result["repairs"] = [dict(row) for row in self.conn.execute("SELECT * FROM repairs WHERE recall_id=? ORDER BY id", (recall_id,))]
        result["reports"] = [dict(row) for row in self.conn.execute("SELECT * FROM regulatory_reports WHERE recall_id=? ORDER BY scope_version", (recall_id,))]
        result["permits"] = [self.permits.permit_dict(row) for row in self.conn.execute("SELECT * FROM permits WHERE recall_id=? ORDER BY id", (recall_id,))]
        return result

    def state(self) -> dict:
        return {"dealers": [dict(row) for row in self.conn.execute("SELECT * FROM dealers ORDER BY id")],
                "vehicles": [dict(row) for row in self.conn.execute("SELECT * FROM vehicles ORDER BY id")],
                "recalls": [self._recall_dict(row) for row in self.conn.execute("SELECT * FROM recalls ORDER BY id DESC")],
                "permits": [self.permits.permit_dict(row) for row in self.conn.execute("SELECT * FROM permits ORDER BY id DESC")],
                "permit_usages": [self.permits.usage_dict(row) for row in self.conn.execute("SELECT * FROM permit_usages ORDER BY id DESC LIMIT 50")],
                "audits": [dict(row) for row in self.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 30")]}

    def seed(self) -> None:
        if not self.conn.execute("SELECT id FROM dealers LIMIT 1").fetchone():
            self.register_dealer("regulator-demo", "regulator", "D-CN", "演示中心", "CN")
        if not self.conn.execute("SELECT id FROM recalls LIMIT 1").fetchone():
            recall = self.create_recall("maker-demo", "manufacturer", "RC-2026-001", "制动管路检查", {"models": ["X1"], "model_years": [2018, 2019], "vin_prefixes": ["LX"], "countries": ["CN"]}, {"version": 1, "description": "更换制动管"})
            self.submit_recall("maker-demo", "manufacturer", recall["id"], recall["revision"])


class Handler(BaseHTTPRequestHandler):
    service: RecallService
    permits: PermitService

    def log_message(self, fmt: str, *args: object) -> None: sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: object) -> None:
        data = json.dumps(body, ensure_ascii=False).encode(); self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def _send_error(self, exc: ApiError) -> None:
        body = {"error": exc.message}
        body.update(exc.details)
        self._send(exc.status, body)

    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        try: return json.loads(self.rfile.read(size)) if size else {}
        except json.JSONDecodeError as exc: raise ApiError(400, "JSON 请求体无效") from exc

    def _parts(self) -> list[str]: return [p for p in urlparse(self.path).path.strip("/").split("/") if p]

    def do_GET(self) -> None:
        try:
            p = self._parts()
            if p in (["health"], ["api", "health"]): out = {"status": "ok"}
            elif p == ["api", "state"]: out = self.service.state()
            elif p == ["api", "permits"]: out = self.permits.list_permits(self.headers.get("X-Actor"), self.headers.get("X-Role"))
            elif len(p) == 3 and p[:2] == ["api", "permits"]:
                out = self.permits.balance(self.headers.get("X-Actor"), self.headers.get("X-Role"), p[2])
            elif len(p) == 3 and p[:2] == ["api", "recalls"]: out = self.service.recall_detail(int(p[2]))
            elif len(p) == 4 and p[:2] == ["api", "recalls"] and p[3] == "unfinished":
                out = self.service.unfinished(self.headers.get("X-Actor"), self.headers.get("X-Role"), int(p[2]))
            elif not p:
                page = (Path(__file__).parent / "static" / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(page))); self.end_headers(); self.wfile.write(page); return
            else: raise ApiError(404, "接口不存在")
            self._send(200, out)
        except ApiError as exc: self._send_error(exc)
        except Exception as exc: self._send(500, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            p, body = self._parts(), self._body(); actor, role = self.headers.get("X-Actor"), self.headers.get("X-Role")
            if p == ["api", "dealers"]: out = self.service.register_dealer(actor, role, body.get("code", ""), body.get("name", ""), body.get("country", ""))
            elif p == ["api", "vehicles"]: out = self.service.register_vehicle(actor, role, body.get("vin", ""), body.get("model", ""), int(body.get("model_year", 0)), body.get("country", ""), body.get("owner_name", ""))
            elif len(p) == 4 and p[:2] == ["api", "vehicles"] and p[3] == "transfer": out = self.service.transfer_vehicle(actor, role, p[2], body.get("country", ""), body.get("owner_name", ""))
            elif p == ["api", "recalls"]: out = self.service.create_recall(actor, role, body.get("campaign_code", ""), body.get("title", ""), body.get("scope", {}), body.get("remedy", {}))
            elif len(p) == 4 and p[:2] == ["api", "recalls"] and p[3] == "submit": out = self.service.submit_recall(actor, role, int(p[2]), int(body.get("expected_version", -1)))
            elif len(p) == 4 and p[:2] == ["api", "recalls"] and p[3] == "review": out = self.service.review_recall(actor, role, int(p[2]), body.get("decision", ""), int(body.get("expected_version", -1)), body.get("note", ""))
            elif len(p) == 4 and p[:2] == ["api", "recalls"] and p[3] == "scope": out = self.service.change_scope(actor, role, int(p[2]), body.get("scope", {}), int(body.get("expected_version", -1)))
            elif len(p) == 4 and p[:2] == ["api", "recalls"] and p[3] == "remedy": out = self.service.revise_remedy(actor, role, int(p[2]), body.get("remedy", {}), int(body.get("expected_version", -1)))
            elif len(p) == 4 and p[:2] == ["api", "recalls"] and p[3] == "parts": out = self.service.add_parts(actor, role, int(p[2]), int(body.get("dealer_id", 0)), int(body.get("remedy_version", 0)), int(body.get("quantity", 0)))
            elif p == ["api", "repairs"]: out = self.service.report_repair(actor, role, int(body.get("recall_id", 0)), body.get("vin", ""), int(body.get("dealer_id", 0)), int(body.get("remedy_version", 0)), body.get("evidence_hash", ""), bool(body.get("evidence_consistent", True)), body.get("border_permit", ""), body.get("idempotency_key", ""))
            elif len(p) == 4 and p[:2] == ["api", "repairs"] and p[3] == "review": out = self.service.review_repair(actor, role, int(p[2]), body.get("decision", ""), body.get("note", ""))
            elif len(p) == 4 and p[:2] == ["api", "repairs"] and p[3] == "withdraw": out = self.service.withdraw_repair(actor, role, int(p[2]), body.get("note", ""))
            elif p == ["api", "permits"]: out = self.permits.issue(actor, role, body.get("permit_code", ""), int(body.get("recall_id", 0)), body.get("model", ""), int(body.get("remedy_version", 0)), body.get("country", ""), int(body.get("total", 0)), body.get("expires_at", ""), body.get("note", ""))
            elif len(p) == 4 and p[:2] == ["api", "permits"] and p[3] == "revoke": out = self.permits.revoke(actor, role, p[2], body.get("note", ""))
            elif len(p) == 4 and p[:2] == ["api", "permits"] and p[3] == "occupy":
                out = self.permits.occupy(actor, role, p[2], body.get("ref_key", ""), body.get("vin", ""), int(body.get("dealer_id", 0)))
            elif len(p) == 5 and p[:2] == ["api", "permit-usages"] and p[3] == "release":
                out = self.permits.release(actor, role, int(p[2]), body.get("reason", ""), body.get("note", ""))
            else: raise ApiError(404, "接口不存在")
            self._send(200, out)
        except ApiError as exc: self._send_error(exc)
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc: self._send(400, {"error": str(exc)})
        except Exception as exc: self._send(500, {"error": str(exc)})


def run(port: int, db_path: str, seed: bool) -> None:
    store = Store(db_path)
    permits = PermitService(store)
    service = RecallService(store, permits)
    if seed: service.seed()
    Handler.service, Handler.permits = service, permits
    print(f"vehicle recall listening on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--port", type=int, default=8213); parser.add_argument("--db", default=str(DB_PATH)); parser.add_argument("--init", action="store_true"); parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    if args.init: Store(args.db).close()
    if args.seed or not args.init: run(args.port, args.db, args.seed)


if __name__ == "__main__": main()
