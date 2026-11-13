"""入湖链路验收测试：覆盖登记快照、校验准入、并发确认与回滚。

只用标准库 unittest，直接驱动 service 层；每个用例先 ``store.reset()``
回到种子数据，避免互相污染。
"""
from __future__ import annotations

import threading
import unittest
from unittest import mock

from app.services.landing import (
    CODE_ARCHIVED_LOCKED,
    CODE_DUPLICATE_BATCH,
    CODE_ILLEGAL_TRANSITION,
    CODE_NO_SNAPSHOT,
    LandingService,
)
from app.services.ods import CODE_BATCH_NOT_READABLE, OdsService
from app.services.snapshot import (
    CODE_ALREADY_COMMITTED,
    CODE_DIGEST_MISSING,
    CODE_DUPLICATE_FILE,
    CODE_EMPTY_FILE,
    CODE_MULTI_FILE_ARCHIVE,
    CODE_ROW_COUNT_MISMATCH,
    CODE_SHARD_CONFLICT,
    STATUS_FAILED,
    STATUS_PASSED,
    snapshot_service,
)
from app.store import store


def registration(**overrides):
    values = {
        "批次编号": "LAND-T1",
        "来源系统": "订单中心",
        "落地路径": "s3://lake/raw/orders/t1.parquet",
        "文件格式": "parquet",
        "接收时间": "2026-11-13T08:00:00+00:00",
        "原始行数": 300000,
        "摘要算法": "sha256",
        "摘要值": "",
        "文件大小": None,
        "source_manifest": {},
    }
    values.update(overrides)
    return values


def confirmation(**overrides):
    values = {
        "digest_type": "sha256",
        "digest_value": "a" * 64,
        "byte_size": 1024,
        "row_count": 300000,
        "file_format": "parquet",
        "received_at": "2026-11-13T08:00:00+00:00",
        "source_manifest": {"file_name": "t1.parquet", "row_count": 300000,
                            "files": [{"name": "t1.parquet"}]},
    }
    values.update(overrides)
    return values


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        store.reset()
        self.svc = LandingService()

    def test_business_fields_fully_persisted(self):
        """验收：文件格式、接收时刻、行数、摘要算法和摘要值登记后仍在。"""
        values = registration(摘要值="b" * 64, 文件大小=2048)
        entry, codes = self.svc.create_entry(values)
        self.assertEqual(codes, [])
        self.assertEqual(entry["文件格式"], "parquet")
        self.assertEqual(entry["接收时间"], "2026-11-13T08:00:00+00:00")
        self.assertEqual(entry["原始行数"], 300000)
        self.assertEqual(entry["摘要算法"], "sha256")
        self.assertEqual(entry["摘要值"], "b" * 64)
        self.assertEqual(entry["文件大小"], 2048)
        self.assertEqual(entry["status"], "草稿")

    def test_missing_required_fields_rejected(self):
        entry, codes = self.svc.create_entry({"批次编号": "LAND-X"})
        self.assertIsNone(entry)
        self.assertIn("来源系统", codes)
        self.assertIn("落地路径", codes)

    def test_duplicate_batch_number_is_retransmission_conflict(self):
        self.svc.create_entry(registration())
        entry, codes = self.svc.create_entry(registration())
        self.assertIsNone(entry)
        self.assertEqual(codes, [CODE_DUPLICATE_BATCH])

    def test_archived_batch_reimport_same_number_rejected(self):
        self.svc.create_entry(registration(批次编号="LAND-ARC"))
        landing_id = self.svc._find_by_batch("LAND-ARC")["id"]
        self.svc.run_action(landing_id, "发起落地", {})
        ok, msg, codes = self.svc.run_action(
            landing_id, "确认已落地", confirmation(digest_value="c" * 64)
        )
        self.assertEqual(codes, [])
        self.svc.run_action(landing_id, "归档批次", {})

        entry, codes = self.svc.create_entry(registration(批次编号="LAND-ARC"))
        self.assertIsNone(entry)
        self.assertEqual(codes, [CODE_ARCHIVED_LOCKED])


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        store.reset()
        self.svc = LandingService()
        self.svc.create_entry(registration())
        self.landing_id = self.svc._find_by_batch("LAND-T1")["id"]

    def test_draft_cannot_skip_to_archived(self):
        entry, msg, codes = self.svc.run_action(self.landing_id, "归档批次", {})
        self.assertIsNone(entry)
        self.assertEqual(codes, [CODE_ILLEGAL_TRANSITION])
        self.assertEqual(store.find("landing", self.landing_id)["status"], "草稿")

    def test_archive_blocked_without_passed_snapshot(self):
        self.svc.run_action(self.landing_id, "发起落地", {})
        # 落地中直接归档同样被拒。
        entry, _, codes = self.svc.run_action(self.landing_id, "归档批次", {})
        self.assertIsNone(entry)
        self.assertEqual(codes, [CODE_ILLEGAL_TRANSITION])

    def test_archived_batch_cannot_reopen_or_reconfirm(self):
        self.svc.run_action(self.landing_id, "发起落地", {})
        self.svc.run_action(self.landing_id, "确认已落地", confirmation())
        self.svc.run_action(self.landing_id, "归档批次", {})

        entry, _, codes = self.svc.run_action(
            self.landing_id, "确认已落地", confirmation(digest_value="d" * 64)
        )
        self.assertIsNone(entry)
        self.assertEqual(codes, [CODE_ARCHIVED_LOCKED])
        entry, _, codes = self.svc.run_action(self.landing_id, "发起落地", {})
        self.assertEqual(codes, [CODE_ARCHIVED_LOCKED])

    def test_archived_requires_passed_snapshot_only(self):
        self.svc.run_action(self.landing_id, "发起落地", {})
        # 校验失败的快照不能支撑归档。
        entry, _, codes = self.svc.run_action(
            self.landing_id, "确认已落地", confirmation(byte_size=0)
        )
        self.assertEqual(codes, [CODE_EMPTY_FILE])
        entry, _, codes = self.svc.run_action(self.landing_id, "归档批次", {})
        self.assertEqual(codes, [CODE_ILLEGAL_TRANSITION])
        self.assertNotEqual(codes, [CODE_NO_SNAPSHOT])


class ValidationTests(unittest.TestCase):
    def setUp(self):
        store.reset()
        self.svc = LandingService()
        self.svc.create_entry(registration())
        self.landing_id = self.svc._find_by_batch("LAND-T1")["id"]
        self.svc.run_action(self.landing_id, "发起落地", {})

    def _confirm(self, **overrides):
        return self.svc.run_action(self.landing_id, "确认已落地", confirmation(**overrides))

    def test_empty_file_distinguishable(self):
        entry, msg, codes = self._confirm(byte_size=0)
        self.assertIsNone(entry)
        self.assertEqual(codes, [CODE_EMPTY_FILE])
        self.assertEqual(store.find("landing", self.landing_id)["status"], "落地中")

    def test_multi_file_archive_distinguishable(self):
        manifest = {"files": [{"name": "a.parquet"}, {"name": "b.parquet"}]}
        entry, _, codes = self._confirm(source_manifest=manifest)
        self.assertEqual(codes, [CODE_MULTI_FILE_ARCHIVE])

    def test_missing_digest_rejected(self):
        entry, _, codes = self._confirm(digest_type="", digest_value="")
        self.assertEqual(codes, [CODE_DIGEST_MISSING])

    def test_row_count_mismatch_rejected(self):
        entry, _, codes = self._confirm(row_count=299999)
        self.assertEqual(codes, [CODE_ROW_COUNT_MISMATCH])
        entry, _, codes = self._confirm(
            source_manifest={"files": [{"name": "t1.parquet"}], "row_count": 1}
        )
        self.assertEqual(codes, [CODE_ROW_COUNT_MISMATCH])

    def test_shard_conflict_same_digest_different_size(self):
        """验收：摘要相同但文件大小不同判成分片冲突。

        首份文件校验失败后失败快照仍在；同名分片以相同摘要、不同大小重传，
        在批次仍处于落地中时即被判为分片冲突，而不是普通重复确认。
        """
        first, _, first_codes = self._confirm(digest_value="e" * 64, byte_size=0)
        self.assertEqual(first_codes, [CODE_EMPTY_FILE])
        entry, _, codes = self._confirm(digest_value="e" * 64, byte_size=2048)
        self.assertIsNone(entry)
        self.assertEqual(codes, [CODE_SHARD_CONFLICT])
        self.assertEqual(store.find("landing", self.landing_id)["status"], "落地中")

    def test_duplicate_file_same_name_same_digest_same_size(self):
        # 落地中阶段：失败后按相同摘要与大小重传，得到可区分的重传结论。
        first, _, first_codes = self._confirm(digest_value="f" * 64, byte_size=1024, row_count=1)
        self.assertEqual(first_codes, [CODE_ROW_COUNT_MISMATCH])
        entry, _, codes = self._confirm(digest_value="f" * 64, byte_size=1024, row_count=300000)
        self.assertEqual(codes, [CODE_DUPLICATE_FILE])

        # 首份完整快照提交后再确认：并发/重复确认统一只接受首份结果。
        ok, _, ok_codes = self._confirm(digest_value="9" * 64, byte_size=1024)
        self.assertEqual(ok_codes, [])
        entry, _, codes = self._confirm(digest_value="9" * 64, byte_size=1024)
        self.assertEqual(codes, [CODE_ALREADY_COMMITTED])

    def test_distinct_codes_for_empty_multi_and_retransmit(self):
        _, _, empty = self._confirm(byte_size=0)
        _, _, multi = self._confirm(
            source_manifest={"files": [{"n": 1}, {"n": 2}]}
        )
        self.assertNotEqual(empty, multi)
        self.assertEqual(empty, [CODE_EMPTY_FILE])
        self.assertEqual(multi, [CODE_MULTI_FILE_ARCHIVE])


class SnapshotAndAuditTests(unittest.TestCase):
    def setUp(self):
        store.reset()
        self.svc = LandingService()
        self.svc.create_entry(registration())
        self.landing_id = self.svc._find_by_batch("LAND-T1")["id"]
        self.svc.run_action(self.landing_id, "发起落地", {})

    def test_passed_snapshot_records_all_required_fields(self):
        entry, msg, codes = self.svc.run_action(
            self.landing_id, "确认已落地", confirmation()
        )
        self.assertEqual(codes, [])
        snapshot = snapshot_service.passed_snapshot("LAND-T1")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["status"], STATUS_PASSED)
        for field in ("digest_type", "digest_value", "byte_size", "completed_at", "source_manifest"):
            self.assertIn(field, snapshot)
        self.assertEqual(snapshot["digest_type"], "sha256")
        self.assertTrue(snapshot["completed_at"])
        self.assertEqual(snapshot["source_manifest"]["row_count"], 300000)

    def test_failed_validation_persists_failed_snapshot_but_no_audit(self):
        audit_before = len(store.rows("auditlog"))
        entry, _, codes = self.svc.run_action(
            self.landing_id, "确认已落地", confirmation(byte_size=0)
        )
        self.assertEqual(codes, [CODE_EMPTY_FILE])
        snapshots = snapshot_service.list_for_batch("LAND-T1")
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["status"], STATUS_FAILED)
        self.assertEqual(snapshots[0]["error_codes"], [CODE_EMPTY_FILE])
        # 失败不推进状态、不写审计。
        self.assertEqual(store.find("landing", self.landing_id)["status"], "落地中")
        self.assertEqual(len(store.rows("auditlog")), audit_before)

    def test_resume_by_snapshot_id_reuses_failed_record(self):
        """验收：恢复时按 snapshot_id 继续，失败快照被原地转正而非新建。"""
        self.svc.run_action(self.landing_id, "确认已落地", confirmation(byte_size=0))
        failed = snapshot_service.list_for_batch("LAND-T1")[0]
        sid = failed["snapshot_id"]

        entry, _, codes = self.svc.run_action(
            self.landing_id,
            "确认已落地",
            confirmation(snapshot_id=sid, byte_size=4096),
        )
        self.assertEqual(codes, [])
        rows = snapshot_service.list_for_batch("LAND-T1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["snapshot_id"], sid)
        self.assertEqual(rows[0]["status"], STATUS_PASSED)
        self.assertEqual(rows[0]["byte_size"], 4096)

    def test_unknown_snapshot_id_rejected(self):
        entry, _, codes = self.svc.run_action(
            self.landing_id,
            "确认已落地",
            confirmation(snapshot_id="SNAP-NOPE-999"),
        )
        # 快照 id 不属于该批次时直接拒绝，不产生新的通过快照。
        self.assertIsNone(entry)
        self.assertIsNone(snapshot_service.passed_snapshot("LAND-T1"))

    def test_rollback_keeps_persisted_original_registration(self):
        """验收：回滚不会删除已持久化的原始登记。"""
        original = store.find("landing", self.landing_id)
        self.assertIsNotNone(original)
        self.svc.run_action(self.landing_id, "确认已落地", confirmation(byte_size=0))
        still = store.find("landing", self.landing_id)
        self.assertIsNotNone(still)
        self.assertEqual(still["批次编号"], "LAND-T1")
        self.assertEqual(still["文件格式"], "parquet")
        self.assertEqual(still["原始行数"], 300000)

    def test_commit_transaction_failure_rolls_back_everywhere(self):
        """提交事务内任一步失败：快照/批次/审计整体回滚，不留半套状态。

        通过让落地记录在事务中途"消失"来制造失败；原始登记本身在事务模块外，
        回滚后必须仍然完整存在，同时留下 FAILED 快照作为恢复锚点。
        """
        from app.services import snapshot as snapshot_module

        real_find = store.find
        calls = {"n": 0}

        def flaky_find(module, entry_id):
            if module == "landing":
                calls["n"] += 1
                # 只在提交事务内的那次查询抛一次故障，回滚后的查询保持正常。
                if calls["n"] == 2:
                    raise RuntimeError("模拟提交中途故障")
            return real_find(module, entry_id)

        audit_before = len(store.rows("auditlog"))
        with mock.patch.object(store, "find", side_effect=flaky_find):
            entry, msg, codes = self.svc.run_action(
                self.landing_id, "确认已落地", confirmation()
            )
        self.assertIsNone(entry)

        # 没有 PASSED 快照、批次仍停在落地中、没有提交审计。
        self.assertIsNone(snapshot_service.passed_snapshot("LAND-T1"))
        self.assertEqual(store.find("landing", self.landing_id)["status"], "落地中")
        self.assertEqual(len(store.rows("auditlog")), audit_before)

        # 原始登记仍在且业务字段完整。
        kept = store.find("landing", self.landing_id)
        self.assertEqual(kept["批次编号"], "LAND-T1")
        self.assertEqual(kept["文件格式"], "parquet")
        self.assertEqual(kept["原始行数"], 300000)

        # 留下 FAILED 恢复锚点，按 snapshot_id 可继续。
        failed = snapshot_service.list_for_batch("LAND-T1")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["status"], STATUS_FAILED)
        self.assertTrue(failed[0]["snapshot_id"])


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        store.reset()
        self.svc = LandingService()
        self.svc.create_entry(registration())
        self.landing_id = self.svc._find_by_batch("LAND-T1")["id"]
        self.svc.run_action(self.landing_id, "发起落地", {})

    def test_concurrent_confirm_accepts_only_first_result(self):
        results: list[tuple] = []

        def worker(digest):
            res = self.svc.run_action(
                self.landing_id, "确认已落地", confirmation(digest_value=digest)
            )
            results.append(res)

        t1 = threading.Thread(target=worker, args=("1" * 64,))
        t2 = threading.Thread(target=worker, args=("2" * 64,))
        t1.start(); t2.start(); t1.join(); t2.join()

        passed = [r for r in results if r[2] == []]
        rejected = [r for r in results if CODE_ALREADY_COMMITTED in r[2]]
        self.assertEqual(len(passed), 1)
        self.assertEqual(len(rejected), 1)
        # 只有一份 PASSED 快照，落地批次只挂一个 snapshot_id。
        snapshots = [
            s for s in store.rows("snapshot")
            if s["batch_no"] == "LAND-T1" and s["status"] == STATUS_PASSED
        ]
        self.assertEqual(len(snapshots), 1)
        landing = store.find("landing", self.landing_id)
        self.assertEqual(landing["snapshot_id"], snapshots[0]["snapshot_id"])
        self.assertEqual(landing["status"], "已落地")


class OdsAdmissionTests(unittest.TestCase):
    def setUp(self):
        store.reset()
        self.landing = LandingService()
        self.ods = OdsService()
        self.landing.create_entry(registration())
        self.landing_id = self.landing._find_by_batch("LAND-T1")["id"]
        entry, _ = self.ods.create_entry({
            "表编号": "ODS-T1", "表名称": "ods_orders", "来源系统": "订单中心",
        })
        self.ods_id = entry["id"]

    def test_admission_rejected_before_validation_passed(self):
        entry, msg, codes = self.ods.admit(self.ods_id, {"批次编号": "LAND-T1"})
        self.assertIsNone(entry)
        self.assertEqual(codes, [CODE_BATCH_NOT_READABLE])
        self.assertEqual(store.find("ods", self.ods_id)["status"], "草稿")

    def test_readable_only_contains_passed_batches(self):
        self.assertEqual(
            [b["批次编号"] for b in self.ods.readable_batches()], ["LAND-0003"]
        )
        self.landing.run_action(self.landing_id, "发起落地", {})
        self.landing.run_action(self.landing_id, "确认已落地", confirmation())
        readable = {b["批次编号"] for b in self.ods.readable_batches()}
        self.assertEqual(readable, {"LAND-0003", "LAND-T1"})

    def test_admission_after_passed_snapshot_and_consistent_judgment(self):
        self.landing.run_action(self.landing_id, "发起落地", {})
        self.landing.run_action(self.landing_id, "确认已落地", confirmation())

        entry, msg, codes = self.ods.admit(self.ods_id, {"批次编号": "LAND-T1"})
        self.assertEqual(codes, [])
        self.assertEqual(entry["status"], "已上线")
        self.assertTrue(entry["可用性判断"]["available"])

        # 落地列表与贴源详情对同一版本给出一致判断。
        landing_view = self.landing.get_entry(self.landing_id)
        self.assertEqual(
            landing_view["可用性判断"]["snapshot_id"],
            entry["可用性判断"]["snapshot_id"],
        )
        self.assertTrue(landing_view["可用性判断"]["available"])

    def test_online_action_requires_passed_snapshot(self):
        entry, msg = self.ods.run_action(self.ods_id, "确认上线")
        self.assertIsNone(entry)

    def test_admit_unknown_batch_rejected(self):
        entry, _, codes = self.ods.admit(self.ods_id, {"批次编号": "LAND-404"})
        self.assertIsNone(entry)

    def test_admit_rolls_back_without_half_audit_on_failure(self):
        # 未通过校验的准入被拒：贴源状态不变，且不产生准入审计。
        audit_before = len(store.rows("auditlog"))
        self.ods.admit(self.ods_id, {"批次编号": "LAND-T1"})
        self.assertEqual(store.find("ods", self.ods_id)["status"], "草稿")
        self.assertEqual(len(store.rows("auditlog")), audit_before)


class StableIdentityTests(unittest.TestCase):
    def setUp(self):
        store.reset()

    def test_landing_and_ods_keep_stable_ids(self):
        svc = LandingService()
        svc.create_entry(registration(批次编号="LAND-ID"))
        landing = svc._find_by_batch("LAND-ID")
        lid = landing["id"]
        svc.run_action(lid, "发起落地", {})
        svc.run_action(lid, "确认已落地", confirmation())
        # 状态流转后主键与批次编号不变。
        self.assertEqual(store.find("landing", lid)["id"], lid)
        self.assertEqual(store.find("landing", lid)["批次编号"], "LAND-ID")

        ods = OdsService()
        entry, _ = ods.create_entry({"表编号": "ODS-ID", "表名称": "t", "来源系统": "s"})
        oid = entry["id"]
        admitted, _, codes = ods.admit(oid, {"批次编号": "LAND-ID"})
        self.assertEqual(codes, [])
        self.assertEqual(store.find("ods", oid)["id"], oid)
        self.assertEqual(store.find("ods", oid)["表编号"], "ODS-ID")


if __name__ == "__main__":
    unittest.main(verbosity=2)
