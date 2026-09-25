import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import RecallService
from permits import ApiError
from store import Store


def iso_future(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")


class PermitFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "p.db"
        self.s = RecallService(Store(self.db_path))
        self.d_cn = self.s.register_dealer("reg", "regulator", "D-CN", "中国中心", "CN")
        self.d_sg = self.s.register_dealer("reg", "regulator", "D-SG", "新加坡中心", "SG")
        recall = self.s.create_recall("maker", "manufacturer", "RC-1", "制动检查",
                                      {"models": ["X"], "model_years": [2018], "vin_prefixes": ["LX"], "countries": ["CN", "SG"]},
                                      {"version": 1, "description": "更换软管"})
        recall = self.s.submit_recall("maker", "manufacturer", recall["id"], recall["revision"])
        self.recall = self.s.review_recall("reg", "regulator", recall["id"], "publish", recall["revision"], "发布")
        self.s.add_parts("maker", "manufacturer", self.recall["id"], self.d_sg["id"], 1, 10)
        self.s.add_parts("maker", "manufacturer", self.recall["id"], self.d_cn["id"], 1, 10)
        self.permit = self.s.permits.issue("reg", "regulator", "P-1", self.recall["id"], "X", "SG", 2, valid_days=30)

    def tearDown(self):
        self.s.store.close()
        self.tmp.cleanup()

    def _vehicle(self, vin, country="CN"):
        # 车辆在 CN、网点在 SG 即构成跨境维修；origin_country=CN 保证在召回范围内
        return self.s.register_vehicle("maker", "manufacturer", vin, "X", 2018, country, "车主")

    def test_cross_border_repair_hold_writeoff_and_kept_record(self):
        v = self._vehicle("LX00001")
        report = self.s.report_repair("dealer", "dealer", self.recall["id"], v["vin"], self.d_sg["id"], 1, "h1", True, "P-1", "repair-1")
        self.assertEqual("held", report["permit_occupation"]["status"])
        bal = self.s.permits.balance("reg", "regulator", "P-1")
        self.assertEqual(1, bal["remaining"])
        self.s.review_repair("reg", "regulator", report["id"], "confirm", "证据一致")
        after = self.s.permits.balance("reg", "regulator", "P-1")
        self.assertEqual(1, after["remaining"])  # 核销不退额度
        detail = self.s.permits.detail("reg", "regulator", "P-1")
        self.assertEqual("written_off", detail["occupations"][0]["status"])  # 记录保留

    def test_flag_and_withdraw_release_quota_keep_record(self):
        v = self._vehicle("LX00002")
        report = self.s.report_repair("dealer", "dealer", self.recall["id"], v["vin"], self.d_sg["id"], 1, "h2", False, "P-1", "repair-2")
        self.s.review_repair("reg", "regulator", report["id"], "confirm")  # 证据不一致 -> flagged
        self.assertEqual(2, self.s.permits.balance("reg", "regulator", "P-1")["remaining"])
        self.assertEqual("released", self.s.permits.detail("reg", "regulator", "P-1")["occupations"][0]["status"])

        v2 = self._vehicle("LX00003")
        report2 = self.s.report_repair("dealer", "dealer", self.recall["id"], v2["vin"], self.d_sg["id"], 1, "h3", True, "P-1", "repair-3")
        self.assertEqual(1, self.s.permits.balance("reg", "regulator", "P-1")["remaining"])
        self.s.withdraw_repair("dealer", "dealer", report2["id"], "取消")
        self.assertEqual(2, self.s.permits.balance("reg", "regulator", "P-1")["remaining"])
        statuses = [o["status"] for o in self.s.permits.detail("reg", "regulator", "P-1")["occupations"]]
        self.assertEqual(["released", "released"], statuses)  # 流水仍保留

    def test_reject_expired_country_remedy_and_shortage_with_conflict_info(self):
        self.s.permits.issue("reg", "regulator", "P-CN", self.recall["id"], "X", "CN", 1, valid_days=30)
        v = self._vehicle("LX00010")
        # 国家不匹配：CN 许可不能给 SG 网点占用
        with self.assertRaises(ApiError) as ctx:
            self.s.report_repair("dealer", "dealer", self.recall["id"], v["vin"], self.d_sg["id"], 1, "h", True, "P-CN", "rx")
        self.assertEqual("国家不匹配", ctx.exception.details["reason"])
        self.assertIn("remaining", ctx.exception.details)  # 返回余量
        self.assertIn("code", ctx.exception.details)       # 返回冲突凭证

        # 过期
        self.s.permits.issue("reg", "regulator", "P-EXP", self.recall["id"], "X", "SG", 1, expires_at=iso_future(-1))
        with self.assertRaises(ApiError) as ctx2:
            self.s.permits.occupy("dealer", "dealer", "P-EXP", self.recall["id"], v["vin"], self.d_sg["id"], "k-exp")
        self.assertEqual("许可已过期", ctx2.exception.details["reason"])

        # 余额不足
        v2, v3, v4 = self._vehicle("LX00011"), self._vehicle("LX00012"), self._vehicle("LX00013")
        self.s.report_repair("dealer", "dealer", self.recall["id"], v2["vin"], self.d_sg["id"], 1, "a", True, "P-1", "ra")
        self.s.permits.occupy("dealer", "dealer", "P-1", self.recall["id"], v3["vin"], self.d_sg["id"], "k-pre")
        with self.assertRaises(ApiError) as ctx3:
            self.s.permits.occupy("dealer", "dealer", "P-1", self.recall["id"], v4["vin"], self.d_sg["id"], "k-short")
        self.assertEqual("余额不足", ctx3.exception.details["reason"])
        self.assertEqual(0, ctx3.exception.details["remaining"])

        # 方案换版：用未占用许可验证版本比对优先于占用动作
        self.s.permits.issue("reg", "regulator", "P-V1", self.recall["id"], "X", "SG", 5, valid_days=30)
        self.s.update_remedy("maker", "manufacturer", self.recall["id"], {"version": 2, "description": "更换硬件"}, self.recall["revision"])
        with self.assertRaises(ApiError) as ctx4:
            self.s.permits.occupy("dealer", "dealer", "P-V1", self.recall["id"], v4["vin"], self.d_sg["id"], "k-ver")
        self.assertEqual("方案已换版", ctx4.exception.details["reason"])

    def test_concurrent_same_permit_only_one_holds(self):
        vehicles = [self._vehicle(f"LX{20000+i}") for i in range(20)]
        results, errors = [], []
        lock = threading.Lock()

        def worker(idx):
            store = Store(self.db_path)  # 独立连接模拟并发
            try:
                svc = RecallService(store)
                out = svc.permits.occupy("dealer", "dealer", "P-1", self.recall["id"],
                                         vehicles[idx]["vin"], self.d_sg["id"], f"concurrent-{idx}")
                with lock:
                    results.append(out)
            except ApiError as exc:
                with lock:
                    errors.append(exc)
            finally:
                store.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(2, len(results))
        self.assertEqual(18, len(errors))
        bal = self.s.permits.balance("reg", "regulator", "P-1")
        self.assertEqual(2, bal["occupied"])
        self.assertEqual(0, bal["remaining"])
        self.assertTrue(all(e.details["remaining"] == 0 for e in errors))

    def test_concurrent_same_request_key_is_idempotent_single_hold(self):
        v = self._vehicle("LX00030")
        # 单额度许可
        self.s.permits.issue("reg", "regulator", "P-ONE", self.recall["id"], "X", "SG", 1, valid_days=10)
        results, errors = [], []
        lock = threading.Lock()

        def worker():
            store = Store(self.db_path)
            try:
                out = RecallService(store).permits.occupy("dealer", "dealer", "P-ONE", self.recall["id"],
                                                          v["vin"], self.d_sg["id"], "same-key")
                with lock:
                    results.append(out["id"])
            except ApiError as exc:
                with lock:
                    errors.append(exc)
            finally:
                store.close()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(8, len(results))
        self.assertEqual(set(results), {results[0]})  # 全部返回同一笔
        self.assertEqual([], errors)
        self.assertEqual(1, self.s.permits.balance("reg", "regulator", "P-ONE")["occupied"])

    def test_role_guard_and_code_required(self):
        with self.assertRaises(ApiError):
            self.s.permits.issue("dealer", "dealer", "P-X", self.recall["id"], "X", "SG", 1)
        v = self._vehicle("LX00040")
        with self.assertRaises(ApiError):
            self.s.report_repair("dealer", "dealer", self.recall["id"], v["vin"], self.d_sg["id"], 1, "h", True, "", "rp")


if __name__ == "__main__":
    unittest.main()
