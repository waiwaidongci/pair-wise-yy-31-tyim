#!/usr/bin/env python3
"""许可判定业务：监管签发可核销额度凭证，网点占用/释放，复核核销。

额度凭证按 (召回, 车型, 修复方案版本, 国家) 签发。占用是原子的：
条件 UPDATE 加占 + (凭证, 请求键) 唯一约束保证同一许可并发只放行一笔；
重复请求键按幂等返回同一笔占用。核销/释放只改流水状态并回补占用量，
流水记录永久保留。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from store import now


class ApiError(Exception):
    def __init__(self, status: int, message: str, details: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.details = status, message, details

    @staticmethod
    def _actor(actor: str | None, role: str | None, allowed: set[str]) -> str:
        if not actor:
            raise ApiError(401, "缺少身份")
        if role not in allowed:
            raise ApiError(403, "角色无权执行此操作")
        return actor


def parse_expiry(expires_at: str, valid_days: int | None = None) -> str:
    if expires_at:
        text = str(expires_at).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ApiError(400, "过期时间格式无效，需 ISO 8601") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    days = int(valid_days) if valid_days is not None else 90
    if days <= 0:
        raise ApiError(400, "有效期天数必须大于零")
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")


def is_expired(permit: sqlite3.Row, stamp: str | None = None) -> bool:
    return permit["expires_at"] <= (stamp or now())


class _HoldRejected(Exception):
    """占用判定失败（余额/并发唯一约束等），由调用方事务负责回滚。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _NullContext:
    """借用调用方已开启的事务，自身不提交也不回滚。"""

    def __enter__(self) -> "_NullContext":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class PermitService:
    def __init__(self, store: object):
        self.store = store
        self.conn = store.conn

    def _permit_row(self, code: str) -> sqlite3.Row:
        row = self.store.get_permit(code.strip().upper())
        if not row:
            raise ApiError(404, "许可凭证不存在")
        return row

    def _recall_row(self, recall_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM recalls WHERE id=?", (recall_id,)).fetchone()
        if not row:
            raise ApiError(404, "召回不存在")
        return row

    def _dealer_row(self, dealer_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM dealers WHERE id=?", (dealer_id,)).fetchone()
        if not row:
            raise ApiError(404, "维修网点不存在")
        return row

    def issue(self, actor: str | None, role: str | None, code: str, recall_id: int, model: str,
              country: str, quota: int, remedy_version: int | None = None,
              expires_at: str = "", valid_days: int | None = None, note: str = "") -> dict:
        actor = ApiError._actor(actor, role, {"regulator"})
        code = str(code).strip().upper()
        if not code:
            raise ApiError(400, "许可编号不能为空")
        try:
            quota = int(quota)
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "额度必须为正整数") from exc
        if quota <= 0:
            raise ApiError(400, "额度必须为正整数")
        model = str(model).strip()
        country = str(country).strip().upper()
        if not model or not country:
            raise ApiError(400, "车型和国家不能为空")
        recall = self._recall_row(int(recall_id))
        if recall["state"] != "published":
            raise ApiError(409, "召回尚未发布，不能签发许可")
        scope = json.loads(recall["scope_json"])
        if model not in scope.get("models", []):
            raise ApiError(409, "车型不在召回范围内")
        version = int(remedy_version if remedy_version is not None else recall["remedy_version"])
        if version != int(recall["remedy_version"]):
            raise ApiError(409, "方案版本与召回当前方案不一致")
        expiry = parse_expiry(expires_at, valid_days)
        try:
            with self.conn:
                permit_id = self.store.insert_permit(code, recall["id"], model, version, country, quota, expiry, actor, note)
                self.store.audit(actor, "permit.issue", "permit", permit_id,
                                 {"code": code, "recall_id": recall["id"], "model": model, "country": country,
                                  "remedy_version": version, "quota": quota, "expires_at": expiry})
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "许可编号已存在") from exc
        return self.detail(actor, role, code)["permit"]

    def revoke(self, actor: str | None, role: str | None, code: str, note: str = "") -> dict:
        """吊销许可：冻结后续占用，已持有占用不受影响（需显式释放或核销）。"""
        actor = ApiError._actor(actor, role, {"regulator"})
        permit = self._permit_row(code)
        with self.conn:
            cur = self.conn.execute("UPDATE permits SET revoked=1 WHERE id=? AND revoked=0", (permit["id"],))
            if cur.rowcount != 1:
                raise ApiError(409, "许可已吊销")
            self.store.audit(actor, "permit.revoke", "permit", permit["id"], {"note": note})
        return self.permit_dict(self.store.get_permit(permit["id"]))

    # ---- 占用 ----
    def _hold(self, permit: sqlite3.Row, request_key: str, dealer: sqlite3.Row, vin: str,
              actor: str, recall_id: int, model: str, remedy_version: int, amount: int = 1,
              managed: bool = True) -> dict:
        stamp = now()
        existing = self.store.find_occupation(permit["id"], request_key)
        if existing is not None:
            return self.occupation_dict(existing, already_exists=True)
        reason = None
        if permit["recall_id"] != int(recall_id):
            reason = "召回不匹配"
        elif permit["model"] != model:
            reason = "车型不匹配"
        elif int(permit["remedy_version"]) != int(remedy_version):
            reason = "方案已换版"
        elif permit["country"] != dealer["country"]:
            reason = "国家不匹配"
        elif permit["revoked"]:
            reason = "许可已吊销"
        elif is_expired(permit, stamp):
            reason = "许可已过期"
        elif int(permit["quota"]) - int(permit["occupied"]) < amount:
            reason = "余额不足"
        if reason is not None:
            raise ApiError(409, f"许可占用被拒绝：{reason}", {"reason": reason, **self.balance_dict(permit)})
        occupation_id: int | None = None
        tx = self.conn if managed else _NullContext()
        try:
            with tx:
                if self.store.guard_occupy(permit["id"], stamp, amount) != 1:
                    raise _HoldRejected("许可不可用或余额不足")
                try:
                    occupation_id = self.store.insert_occupation(permit["id"], request_key, dealer["id"], vin, actor, amount)
                except sqlite3.IntegrityError as exc:
                    # 同一(许可,请求键) 已存在：并发重复请求，回退加占，按幂等返回
                    raise _HoldRejected("duplicate") from exc
        except _HoldRejected:
            if not managed:
                raise
            existing = self.store.find_occupation(permit["id"], request_key)
            if existing is not None:
                return self.occupation_dict(existing, already_exists=True)
            fresh = self.store.get_permit(permit["id"], "id")
            raise ApiError(409, "许可占用被拒绝：并发冲突，余量可能已被占用",
                           {"reason": "并发冲突", **self.balance_dict(fresh)})
        existing = self.store.find_occupation(permit["id"], request_key)
        if existing is not None and occupation_id is not None and existing["id"] != occupation_id:
            return self.occupation_dict(existing, already_exists=True)
        self.store.audit(actor, "permit.hold", "permit_occupation", occupation_id,
                         {"permit_id": permit["id"], "request_key": request_key, "vin": vin, "dealer_id": dealer["id"]})
        return self.occupation_dict(self.store.get_occupation(occupation_id))

    def occupy(self, actor: str | None, role: str | None, code: str, recall_id: int, vin: str,
               dealer_id: int, request_key: str, amount: int = 1) -> dict:
        actor = ApiError._actor(actor, role, {"dealer"})
        if not request_key:
            raise ApiError(400, "占用请求键不能为空")
        permit = self._permit_row(code)
        recall = self._recall_row(int(recall_id))
        dealer = self._dealer_row(int(dealer_id))
        if not dealer["active"]:
            raise ApiError(409, "维修网点已停用")
        vehicle = self.conn.execute("SELECT * FROM vehicles WHERE vin=?", (str(vin).upper().strip(),)).fetchone()
        if not vehicle:
            raise ApiError(404, "车辆不存在")
        if int(amount) < 1:
            raise ApiError(400, "占用数量必须大于零")
        return self._hold(permit, request_key, dealer, vehicle["vin"], actor,
                          int(recall_id), vehicle["model"], int(recall["remedy_version"]), int(amount))

    def hold_for_repair(self, permit_code: str, actor: str, recall_id: int, vehicle: sqlite3.Row,
                        dealer: sqlite3.Row, remedy_version: int, request_key: str) -> dict:
        """维修流程内部调用：复用报修外层事务，按报修幂等键占用，返回占用流水。"""
        permit = self._permit_row(permit_code)
        try:
            return self._hold(permit, request_key, dealer, vehicle["vin"], actor,
                              recall_id, vehicle["model"], int(remedy_version), managed=False)
        except _HoldRejected as exc:
            raise ApiError(409, f"许可占用被拒绝：{exc.reason}",
                           {"reason": exc.reason, **self.balance_dict(self.store.get_permit(permit["id"], "id"))}) from exc

    # ---- 释放 / 核销（流水永久保留） ----
    def release(self, actor: str | None, role: str | None, occupation_id: int, reason: str = "withdrawn") -> dict:
        actor = ApiError._actor(actor, role, {"dealer", "regulator"})
        occupation = self.store.get_occupation(int(occupation_id))
        if not occupation:
            raise ApiError(404, "占用记录不存在")
        with self.conn:
            if self.store.transition_occupation(occupation["id"], "released", reason):
                self.store.adjust_occupied(occupation["permit_id"], -int(occupation["amount"]))
            self.store.audit(actor, "permit.release", "permit_occupation", occupation["id"], {"reason": reason})
        return self.occupation_dict(self.store.get_occupation(occupation["id"]))

    def settle_for_repair(self, repair_id: int, write_off: bool, reason: str, actor: str) -> dict | None:
        """维修复核/撤回时在调用方事务内调用，返回新的占用流水，无占用返回 None。"""
        held = self.store.held_occupation_for_repair(repair_id)
        if held is None:
            return None
        if write_off:
            self.store.transition_occupation(held["id"], "written_off", reason)
        else:
            self.store.transition_occupation(held["id"], "released", reason)
            self.store.adjust_occupied(held["permit_id"], -int(held["amount"]))
        self.store.audit(actor, "permit.writeoff" if write_off else "permit.release",
                         "permit_occupation", held["id"], {"repair_id": repair_id, "reason": reason})
        return self.occupation_dict(self.store.get_occupation(held["id"]))

    def link_repair(self, occupation_id: int, repair_id: int) -> None:
        self.store.link_occupation_repair(occupation_id, repair_id)

    # ---- 查询 ----
    def balance(self, actor: str | None, role: str | None, code: str) -> dict:
        ApiError._actor(actor, role, {"regulator", "dealer", "manufacturer"})
        return self.balance_dict(self._permit_row(code))

    def balance_dict(self, permit: sqlite3.Row) -> dict:
        counts = self.store.occupation_counts(permit["id"])
        return {"permit_id": permit["id"], "code": permit["code"], "recall_id": permit["recall_id"],
                "model": permit["model"], "remedy_version": permit["remedy_version"], "country": permit["country"],
                "quota": permit["quota"], "occupied": permit["occupied"], "remaining": permit["quota"] - permit["occupied"],
                "revoked": bool(permit["revoked"]), "expired": is_expired(permit), "expires_at": permit["expires_at"],
                "counts": counts}

    def detail(self, actor: str | None, role: str | None, code: str) -> dict:
        ApiError._actor(actor, role, {"regulator", "dealer", "manufacturer"})
        permit = self._permit_row(code)
        return {"permit": self.permit_dict(permit),
                "occupations": [self.occupation_dict(r) for r in self.store.list_occupations(permit["id"])]}

    def list(self, actor: str | None, role: str | None, recall_id: int | None = None) -> dict:
        ApiError._actor(actor, role, {"regulator", "dealer", "manufacturer"})
        rows = self.store.list_permits(int(recall_id) if recall_id else None)
        return {"permits": [self.permit_dict(r) for r in rows]}

    def permit_dict(self, row: sqlite3.Row) -> dict:
        return {"id": row["id"], "code": row["code"], "recall_id": row["recall_id"], "model": row["model"],
                "remedy_version": row["remedy_version"], "country": row["country"], "quota": row["quota"],
                "occupied": row["occupied"], "remaining": row["quota"] - row["occupied"],
                "revoked": bool(row["revoked"]), "expired": is_expired(row), "expires_at": row["expires_at"],
                "note": row["note"], "issued_by": row["issued_by"], "issued_at": row["issued_at"]}

    @staticmethod
    def occupation_dict(row: sqlite3.Row, already_exists: bool = False) -> dict:
        return {"id": row["id"], "permit_id": row["permit_id"], "repair_id": row["repair_id"],
                "request_key": row["request_key"], "dealer_id": row["dealer_id"], "vin": row["vin"],
                "amount": row["amount"], "status": row["status"], "reason": row["reason"],
                "created_by": row["created_by"], "created_at": row["created_at"], "updated_at": row["updated_at"],
                "already_exists": already_exists}
