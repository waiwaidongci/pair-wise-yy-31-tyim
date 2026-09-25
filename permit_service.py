"""许可判定业务：监管签发额度、网点占用凭证、核销与释放、余额查询。"""
from __future__ import annotations

import sqlite3

from store import ApiError, now, parse_ts


class PermitService:
    def __init__(self, store):
        self.store, self.conn = store, store.conn

    @staticmethod
    def _actor(actor: str | None, role: str | None, allowed: set[str]) -> str:
        if not actor:
            raise ApiError(401, "缺少身份")
        if role not in allowed:
            raise ApiError(403, "角色无权执行此操作")
        return actor

    def _row(self, table: str, identity: object, column: str = "id") -> sqlite3.Row:
        row = self.conn.execute(f"SELECT * FROM {table} WHERE {column}=?", (identity,)).fetchone()
        if not row:
            raise ApiError(404, "对象不存在")
        return row

    # ---- 监管签发 ----------------------------------------------------------------

    def issue(self, actor: str | None, role: str | None, permit_code: str, recall_id: int, model: str,
              remedy_version: int, country: str, total: int, expires_at: str, note: str = "",
              allow_expired: bool = False) -> dict:
        actor = self._actor(actor, role, {"regulator"})
        permit_code = (permit_code or "").strip()
        model, country, expires_at = (model or "").strip(), (country or "").strip(), (expires_at or "").strip()
        if not permit_code:
            raise ApiError(400, "许可编号不能为空")
        if not model or not country:
            raise ApiError(400, "车型和适用国家不能为空")
        try:
            total = int(total)
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "额度必须是正整数") from exc
        if total <= 0:
            raise ApiError(400, "额度必须是正整数")
        try:
            expire_dt = parse_ts(expires_at)
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "有效期格式无效，需要 ISO 时间") from exc
        if expire_dt <= parse_ts(now()) and not allow_expired:
            raise ApiError(400, "有效期必须晚于当前时间")
        recall = self._row("recalls", recall_id)
        if recall["state"] != "published":
            raise ApiError(409, "只能为已发布召回签发许可")
        if int(remedy_version) != int(recall["remedy_version"]):
            raise ApiError(409, "方案版本与召回当前版本不一致", {"current_remedy_version": recall["remedy_version"]})
        stamp = now()
        try:
            with self.store.transaction():
                cur = self.conn.execute("""INSERT INTO permits(permit_code,recall_id,model,remedy_version,country,total,remaining,
                    expires_at,note,issued_by,issued_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                                        (permit_code, recall_id, model, int(remedy_version), country, total, total,
                                         expires_at, note, actor, stamp, stamp))
                self.store.audit(actor, "permit.issue", "permit", cur.lastrowid,
                                 {"permit_code": permit_code, "recall_id": recall_id, "model": model,
                                  "remedy_version": remedy_version, "country": country, "total": total,
                                  "expires_at": expires_at})
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "许可编号已存在") from exc
        return self.permit_dict(self._row("permits", cur.lastrowid))

    def revoke(self, actor: str | None, role: str | None, permit_code: str, note: str = "") -> dict:
        """撤回许可：释放全部占用中的凭证，许可作废。"""
        actor = self._actor(actor, role, {"regulator"})
        permit = self._row("permits", permit_code.strip(), "permit_code")
        with self.store.transaction():
            self._settle_held_locked(permit, "released", "revoked", actor, note)
            self.conn.execute("UPDATE permits SET status='revoked',updated_at=? WHERE id=?", (now(), permit["id"]))
            self.store.audit(actor, "permit.revoke", "permit", permit["id"], {"note": note})
        return self.permit_dict(self._row("permits", permit["id"]))

    # ---- 网点占用 ----------------------------------------------------------------

    def occupy(self, actor: str | None, role: str | None, permit_code: str, ref_key: str,
               vin: str, dealer_id: int) -> dict:
        actor = self._actor(actor, role, {"dealer"})
        with self.store.transaction():
            usage_id = self._occupy_locked(permit_code, ref_key, vin, dealer_id, actor)
        return self.usage_dict(self._row("permit_usages", usage_id))

    def _occupy_locked(self, permit_code: str, ref_key: str, vin: str, dealer_id: int, actor: str) -> int:
        """在调用方事务内执行占用；所有拒绝路径返回冲突凭证与余量。返回 usage id。"""
        permit_code, ref_key = (permit_code or "").strip(), (ref_key or "").strip()
        if not permit_code:
            raise ApiError(403, "跨境维修需要有效许可")
        if not ref_key:
            raise ApiError(400, "占用引用键不能为空")
        permit = self.conn.execute("SELECT * FROM permits WHERE permit_code=?", (permit_code,)).fetchone()
        if not permit:
            raise ApiError(404, "许可不存在", {"permit_code": permit_code})

        def conflict(reason: str, extra: dict | None = None, status: int = 409) -> None:
            details = self._conflict_payload(permit)
            details.update(extra or {})
            raise ApiError(status, reason, details)

        if permit["status"] == "revoked":
            conflict("许可已撤回，不能继续占用")
        if parse_ts(permit["expires_at"]) < parse_ts(now()):
            conflict("许可已过期，不能继续占用")
        vehicle = self.conn.execute("SELECT * FROM vehicles WHERE vin=?", (vin.upper(),)).fetchone()
        if not vehicle:
            raise ApiError(404, "车辆不存在", {"vin": vin})
        if vehicle["model"] != permit["model"]:
            conflict("许可车型与报修车辆不匹配")
        dealer = self._row("dealers", dealer_id)
        if dealer["country"] != permit["country"]:
            conflict("许可适用国家与维修网点国家不匹配")
        recall = self._row("recalls", permit["recall_id"])
        if int(recall["remedy_version"]) != int(permit["remedy_version"]):
            conflict("召回修复方案已换版，许可对应旧方案", {"current_remedy_version": recall["remedy_version"]})

        # 同一引用键重复请求：幂等返回已有凭证（并发重复占用只放行一笔）。
        existing = self.conn.execute("SELECT * FROM permit_usages WHERE permit_id=? AND ref_key=?",
                                     (permit["id"], ref_key)).fetchone()
        if existing:
            conflict("同一引用键已有占用凭证", {"conflict_usage": self.usage_dict(existing)})
        if permit["remaining"] < 1:
            conflict("许可余量不足")
        stamp = now()
        cur = self.conn.execute("""UPDATE permits SET remaining=remaining-1,occupied=occupied+1,updated_at=?
                                   WHERE id=? AND remaining>0 AND status='active'""", (stamp, permit["id"]))
        if cur.rowcount != 1:
            conflict("许可余量不足或已失效")
        usage = self.conn.execute("""INSERT INTO permit_usages(permit_id,ref_key,recall_id,vehicle_id,dealer_id,amount,
                                  status,occupied_by,occupied_at) VALUES(?,?,?,?,?,1,'held',?,?)""",
                                  (permit["id"], ref_key, permit["recall_id"], vehicle["id"], dealer_id, actor, stamp))
        self.store.audit(actor, "permit.occupy", "permit_usage", usage.lastrowid,
                         {"permit_code": permit_code, "ref_key": ref_key, "vin": vehicle["vin"],
                          "remaining": permit["remaining"] - 1})
        return usage.lastrowid

    # ---- 核销 / 释放 -------------------------------------------------------------

    def release(self, actor: str | None, role: str | None, usage_id: int, reason: str, note: str = "") -> dict:
        actor = self._actor(actor, role, {"dealer", "regulator"})
        if reason not in {"return", "withdraw", "revoked"}:
            raise ApiError(400, "释放原因只能是 return 或 withdraw（监管撤回为 revoked）")
        usage = self._row("permit_usages", usage_id)
        with self.store.transaction():
            self._settle_locked(usage, "released", reason, actor, note)
        return self.usage_dict(self._row("permit_usages", usage_id))

    def _settle_held_locked(self, permit: sqlite3.Row, new_status: str, reason: str, actor: str, note: str) -> None:
        held = self.conn.execute("SELECT * FROM permit_usages WHERE permit_id=? AND status='held'", (permit["id"],)).fetchall()
        for usage in held:
            self._settle_locked(usage, new_status, reason, actor, note)

    def _settle_locked(self, usage: sqlite3.Row, new_status: str, reason: str, actor: str, note: str) -> None:
        """在调用方事务内完成核销或释放；释放回补余量，核销不回补。记录始终保留。"""
        if usage["status"] != "held":
            raise ApiError(409, "凭证已处理，不能重复操作", {"usage_id": usage["id"], "status": usage["status"]})
        stamp = now()
        self.conn.execute("""UPDATE permit_usages SET status=?,release_reason=?,finished_by=?,finished_at=? WHERE id=?""",
                          (new_status, reason if new_status == "released" else "", actor, stamp, usage["id"]))
        permit = self._row("permits", usage["permit_id"])
        if new_status == "released":
            self.conn.execute("UPDATE permits SET remaining=remaining+?,occupied=occupied-?,updated_at=? WHERE id=?",
                              (usage["amount"], usage["amount"], stamp, permit["id"]))
        else:
            self.conn.execute("UPDATE permits SET occupied=occupied-?,written_off=written_off+?,updated_at=? WHERE id=?",
                              (usage["amount"], usage["amount"], stamp, permit["id"]))
        self.store.audit(actor, f"permit.{new_status}", "permit_usage", usage["id"],
                         {"permit_id": permit["id"], "reason": reason, "note": note})

    # ---- 查询 --------------------------------------------------------------------

    def balance(self, actor: str | None, role: str | None, permit_code: str) -> dict:
        self._actor(actor, role, {"regulator", "dealer", "manufacturer"})
        permit = self._row("permits", permit_code.strip(), "permit_code")
        return self._balance_payload(permit)

    def list_permits(self, actor: str | None, role: str | None) -> dict:
        self._actor(actor, role, {"regulator", "dealer", "manufacturer"})
        rows = self.conn.execute("SELECT * FROM permits ORDER BY id DESC").fetchall()
        return {"permits": [self.permit_dict(row) for row in rows]}

    def _balance_payload(self, permit: sqlite3.Row) -> dict:
        counts = {r["status"]: r["n"] for r in self.conn.execute(
            "SELECT status,COUNT(*) n FROM permit_usages WHERE permit_id=? GROUP BY status", (permit["id"],))}
        recent = [self.usage_dict(r) for r in self.conn.execute(
            "SELECT * FROM permit_usages WHERE permit_id=? ORDER BY id DESC LIMIT 20", (permit["id"],))]
        return {"permit": self.permit_dict(permit),
                "held_count": counts.get("held", 0), "written_off_count": counts.get("written_off", 0),
                "released_count": counts.get("released", 0), "recent_usages": recent}

    def _conflict_payload(self, permit: sqlite3.Row, usage: sqlite3.Row | None = None) -> dict:
        payload = self._balance_payload(permit)
        out = {"permit": payload["permit"], "remaining": permit["remaining"],
               "held_count": payload["held_count"], "usage_history": payload["recent_usages"]}
        if usage is not None:
            out["conflict_usage"] = self.usage_dict(usage)
        return out

    def permit_dict(self, row: sqlite3.Row) -> dict:
        expired = parse_ts(row["expires_at"]) < parse_ts(now())
        return {"id": row["id"], "permit_code": row["permit_code"], "recall_id": row["recall_id"],
                "model": row["model"], "remedy_version": row["remedy_version"], "country": row["country"],
                "total": row["total"], "remaining": row["remaining"], "occupied": row["occupied"],
                "written_off": row["written_off"], "status": row["status"], "expired": expired,
                "expires_at": row["expires_at"], "note": row["note"],
                "issued_by": row["issued_by"], "issued_at": row["issued_at"]}

    def usage_dict(self, row: sqlite3.Row) -> dict:
        return {"id": row["id"], "permit_id": row["permit_id"], "ref_key": row["ref_key"],
                "recall_id": row["recall_id"], "vehicle_id": row["vehicle_id"], "dealer_id": row["dealer_id"],
                "repair_id": row["repair_id"], "amount": row["amount"], "status": row["status"],
                "release_reason": row["release_reason"], "occupied_by": row["occupied_by"],
                "occupied_at": row["occupied_at"], "finished_by": row["finished_by"], "finished_at": row["finished_at"]}
