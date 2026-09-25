import sys, tempfile, threading, unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, RecallService, Store
from permit_service import PermitService


class PermitFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "p.db")
        self.svc = RecallService(self.store)
        self.permits = PermitService(self.store)
        self.dealer_sg = self.svc.register_dealer("reg", "regulator", "D-SG", "新加坡中心", "SG")
        r = self.svc.create_recall("maker", "manufacturer", "RC-P", "制动检查",
                                   {"models": ["X"], "model_years": [2018], "vin_prefixes": ["LX"], "countries": ["CN", "SG"]},
                                   {"version": 1, "description": "更换软管"})
        r = self.svc.submit_recall("maker", "manufacturer", r["id"], r["revision"])
        self.recall = self.svc.review_recall("reg", "regulator", r["id"], "publish", r["revision"], "同意发布")
        self.expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

    def tearDown(self): self.store.close(); self.tmp.cleanup()

    def _vehicle(self, vin, country="SG"):
        v = self.svc.register_vehicle("maker", "manufacturer", vin, "X", 2018, "CN", "车主")
        self.svc.transfer_vehicle("dealer", "dealer", vin, country, "Owner")
        return v

    def _issue(self, code="PX-1", country="SG", total=3, remedy=1, expires=None, allow_expired=False):
        return self.permits.issue("reg", "regulator", code, self.recall["id"], "X", remedy, country, total,
                                  expires or self.expires, "测试许可", allow_expired=allow_expired)

    # ---- 并发：同一许可只放行总额度笔；同一引用键只放行一笔 ----

    def test_concurrent_occupy_only_total_pass(self):
        self._issue(total=3)
        for i in range(10):
            self._vehicle(f"LX{i:05d}")
        results, errors = [], []

        def occupy(i):
            try:
                u = self.permits.occupy("dealer", "dealer", "PX-1", f"ref-{i}", f"LX{i:05d}", self.dealer_sg["id"])
                results.append(u)
            except ApiError as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(occupy, range(10)))
        self.assertEqual(3, len(results))
        self.assertEqual(7, len(errors))
        for exc in errors:
            self.assertEqual(409, exc.status)
            self.assertIn("remaining", exc.details)
            self.assertEqual(0, exc.details["permit"]["remaining"])
            self.assertIn("usage_history", exc.details)
        balance = self.permits.balance("reg", "regulator", "PX-1")
        self.assertEqual(0, balance["permit"]["remaining"])
        self.assertEqual(3, balance["held_count"])

    def test_same_ref_concurrent_only_one_held(self):
        self._issue(total=5)
        self._vehicle("LX00010")
        outcomes = []

        def occupy(_):
            try:
                outcomes.append(("ok", self.permits.occupy("dealer", "dealer", "PX-1", "dup-ref", "LX00010", self.dealer_sg["id"])))
            except ApiError as exc:
                outcomes.append(("err", exc))

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(occupy, range(10)))
        oks = [o for o in outcomes if o[0] == "ok"]
        errs = [o for o in outcomes if o[0] == "err"]
        self.assertEqual(1, len(oks))
        self.assertEqual(9, len(errs))
        self.assertEqual("同一引用键已有占用凭证", errs[0][1].message)
        self.assertEqual("held", errs[0][1].details["conflict_usage"]["status"])
        balance = self.permits.balance("reg", "regulator", "PX-1")
        self.assertEqual(4, balance["permit"]["remaining"])
        self.assertEqual(1, balance["held_count"])

    # ---- 拒绝路径：过期、国家不匹配、方案换版、余量不足 ----

    def test_expired_permit_rejected(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self._issue(code="OLD", total=1, expires=past, allow_expired=True)
        self._vehicle("LX00020")
        with self.assertRaises(ApiError) as ctx:
            self.permits.occupy("dealer", "dealer", "OLD", "ref-x", "LX00020", self.dealer_sg["id"])
        self.assertEqual(409, ctx.exception.status)
        self.assertIn("过期", ctx.exception.message)
        self.assertTrue(ctx.exception.details["permit"]["expired"])
        self.assertEqual(1, ctx.exception.details["permit"]["remaining"])

    def test_country_mismatch_rejected(self):
        self._issue(code="JP", country="JP", total=2)
        self._vehicle("LX00030")
        with self.assertRaises(ApiError) as ctx:
            self.permits.occupy("dealer", "dealer", "JP", "ref-x", "LX00030", self.dealer_sg["id"])
        self.assertIn("国家", ctx.exception.message)
        self.assertEqual("JP", ctx.exception.details["permit"]["country"])

    def test_model_mismatch_rejected(self):
        self._issue(code="MY")
        v = self.svc.register_vehicle("maker", "manufacturer", "LX00040", "Y", 2018, "SG", "车主")
        with self.assertRaises(ApiError) as ctx:
            self.permits.occupy("dealer", "dealer", "MY", "ref-x", v["vin"], self.dealer_sg["id"])
        self.assertIn("车型", ctx.exception.message)

    def test_remedy_revised_rejects_old_permit(self):
        self._issue(code="V1", total=3)
        revised = self.svc.revise_remedy("maker", "manufacturer", self.recall["id"], {"version": 2, "description": "更换加强管"}, self.recall["revision"])
        self.assertEqual(2, revised["remedy_version"])
        self._vehicle("LX00050")
        with self.assertRaises(ApiError) as ctx:
            self.permits.occupy("dealer", "dealer", "V1", "ref-x", "LX00050", self.dealer_sg["id"])
        self.assertIn("换版", ctx.exception.message)
        self.assertEqual(2, ctx.exception.details["current_remedy_version"])

    def test_insufficient_balance_then_release_restores(self):
        self._issue(total=1)
        self._vehicle("LX00060"); self._vehicle("LX00061")
        u1 = self.permits.occupy("dealer", "dealer", "PX-1", "r1", "LX00060", self.dealer_sg["id"])
        with self.assertRaises(ApiError) as ctx:
            self.permits.occupy("dealer", "dealer", "PX-1", "r2", "LX00061", self.dealer_sg["id"])
        self.assertIn("余量不足", ctx.exception.message)
        self.assertEqual(0, ctx.exception.details["remaining"])
        self.permits.release("dealer", "dealer", u1["id"], "return", "退回")
        u2 = self.permits.occupy("dealer", "dealer", "PX-1", "r2", "LX00061", self.dealer_sg["id"])
        self.assertEqual("held", u2["status"])

    def test_writeoff_and_withdraw_keep_history(self):
        self._issue(total=2)
        self._vehicle("LX00070"); self._vehicle("LX00071")
        u1 = self.permits.occupy("dealer", "dealer", "PX-1", "w1", "LX00070", self.dealer_sg["id"])
        u2 = self.permits.occupy("dealer", "dealer", "PX-1", "w2", "LX00071", self.dealer_sg["id"])
        with self.store.transaction():
            row = self.store.conn.execute("SELECT * FROM permit_usages WHERE id=?", (u1["id"],)).fetchone()
            self.permits._settle_locked(row, "written_off", "confirm", "reg", "核销")
        self.permits.release("reg", "regulator", u2["id"], "withdraw", "撤回")
        balance = self.permits.balance("reg", "regulator", "PX-1")
        self.assertEqual(1, balance["permit"]["remaining"])
        self.assertEqual(1, balance["written_off_count"])
        self.assertEqual(1, balance["released_count"])
        # 记录仍保留
        statuses = {u["id"]: u["status"] for u in balance["recent_usages"]}
        self.assertEqual("written_off", statuses[u1["id"]])
        self.assertEqual("released", statuses[u2["id"]])
        with self.assertRaises(ApiError):  # 已处理凭证不能重复释放
            self.permits.release("reg", "regulator", u2["id"], "withdraw")

    def test_revoke_releases_held(self):
        self._issue(total=2)
        self._vehicle("LX00080"); self._vehicle("LX00081")
        self.permits.occupy("dealer", "dealer", "PX-1", "k1", "LX00080", self.dealer_sg["id"])
        self.permits.occupy("dealer", "dealer", "PX-1", "k2", "LX00081", self.dealer_sg["id"])
        revoked = self.permits.revoke("reg", "regulator", "PX-1", "监管撤回")
        self.assertEqual("revoked", revoked["status"])
        self.assertEqual(2, revoked["remaining"])
        with self.assertRaises(ApiError) as ctx:
            self.permits.occupy("dealer", "dealer", "PX-1", "k3", "LX00080", self.dealer_sg["id"])
        self.assertIn("撤回", ctx.exception.message)

    def test_cross_border_repair_end_to_end(self):
        self._issue(code="CB-1", total=1)
        self._vehicle("LX00090")
        self.svc.add_parts("maker", "manufacturer", self.recall["id"], self.dealer_sg["id"], 1, 1)
        # 无许可的跨境报修被拒
        with self.assertRaises(ApiError) as ctx:
            self.svc.report_repair("dealer", "dealer", self.recall["id"], "LX00090", self.dealer_sg["id"], 1, "h1", True, "", "rep-1")
        self.assertEqual(403, ctx.exception.status)
        report = self.svc.report_repair("dealer", "dealer", self.recall["id"], "LX00090", self.dealer_sg["id"], 1, "h1", False, "CB-1", "rep-1")
        # 同幂等键重试：不重复占用
        again = self.svc.report_repair("dealer", "dealer", self.recall["id"], "LX00090", self.dealer_sg["id"], 1, "h1", False, "CB-1", "rep-1")
        self.assertEqual(report["id"], again["id"])
        self.assertEqual(0, self.permits.balance("reg", "regulator", "CB-1")["permit"]["remaining"])
        # 复核打回（证据不一致）：额度退回
        flagged = self.svc.review_repair("reg", "regulator", report["id"], "confirm", "证据有问题")
        self.assertEqual("flagged", flagged["status"])
        self.assertEqual(1, self.permits.balance("reg", "regulator", "CB-1")["permit"]["remaining"])

    def test_withdraw_repair_releases_permit(self):
        self._issue(code="WD-1", total=1)
        self._vehicle("LX00100")
        self.svc.add_parts("maker", "manufacturer", self.recall["id"], self.dealer_sg["id"], 1, 1)
        report = self.svc.report_repair("dealer", "dealer", self.recall["id"], "LX00100", self.dealer_sg["id"], 1, "h", True, "WD-1", "w-rep")
        withdrawn = self.svc.withdraw_repair("dealer", "dealer", report["id"], "车主改约")
        self.assertEqual("withdrawn", withdrawn["status"])
        balance = self.permits.balance("reg", "regulator", "WD-1")
        self.assertEqual(1, balance["permit"]["remaining"])
        self.assertEqual(1, balance["released_count"])
        with self.assertRaises(ApiError):
            self.svc.withdraw_repair("dealer", "dealer", report["id"])


if __name__ == "__main__": unittest.main()
