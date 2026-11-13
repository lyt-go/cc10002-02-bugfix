"""原始落地业务规则。

修复要点：

* 登记时完整保存业务字段（文件格式、接收时刻、行数、摘要算法/值、大小、清单），
  不再只留来源系统与路径；原始登记一旦提交即持久化，后续回滚不删除它。
* 状态流转由校验结果决定：草稿不能跨到归档；归档后不能再重开文件/确认；
  缺摘要、行数不符、空文件、压缩包多文件等都会被拒在"已落地"之前。
* 确认已落地走共享校验快照（``app.services.snapshot``），
  同一批次并发确认只接受首份完整快照。
"""
from __future__ import annotations

from typing import Any

from app.services.snapshot import (
    CODE_ALREADY_COMMITTED,
    CODE_MESSAGES,
    SnapshotService,
    snapshot_service,
)
from app.store import store

MODULE = "landing"
REQUIRED_FIELDS = ["批次编号", "来源系统", "落地路径"]
# 登记快照要求随单保存的完整业务字段。
BUSINESS_FIELDS = [
    "文件格式",
    "接收时间",
    "原始行数",
    "摘要算法",
    "摘要值",
    "文件大小",
    "source_manifest",
]
STATUS_ORDER = ["草稿", "落地中", "已落地", "已归档"]

CODE_ARCHIVED_LOCKED = "ARCHIVED_BATCH_LOCKED"
CODE_DUPLICATE_BATCH = "DUPLICATE_BATCH_NO"
CODE_ILLEGAL_TRANSITION = "ILLEGAL_TRANSITION"
CODE_NO_SNAPSHOT = "NO_PASSED_SNAPSHOT"

EXTRA_MESSAGES = {
    CODE_ARCHIVED_LOCKED: "批次已归档，归档文件不可再打开或重复导入同编号文件",
    CODE_DUPLICATE_BATCH: "同编号文件已登记为未归档批次，同名文件重传请走原批次确认",
    CODE_ILLEGAL_TRANSITION: "当前状态不允许执行该动作，状态只能按 草稿→落地中→已落地→已归档 推进",
    CODE_NO_SNAPSHOT: "尚无校验通过的完整快照，不能归档",
}


def message_for(code: str) -> str:
    return CODE_MESSAGES.get(code) or EXTRA_MESSAGES.get(code) or code


class LandingService:
    def __init__(self, snapshots: SnapshotService | None = None) -> None:
        self.snapshots = snapshots or snapshot_service

    # -- 查询 -------------------------------------------------------------

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("批次编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        # 列表与详情使用同一套快照可用性判断，保证两处结论一致。
        items = [self._with_availability(dict(row)) for row in rows]
        total = len(items)
        start = max(page - 1, 0) * size
        return items[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return self._with_availability(dict(entry))

    def _with_availability(self, entry: dict[str, Any]) -> dict[str, Any]:
        batch_no = str(entry.get("批次编号", ""))
        entry["可用性判断"] = self.snapshots.availability(batch_no)
        entry["批次状态"] = entry.get("status")
        return entry

    # -- 登记 -------------------------------------------------------------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing

        batch_no = str(values["批次编号"]).strip()
        with store.lock:
            existing = self._find_by_batch(batch_no)
            if existing is not None:
                if existing.get("status") == STATUS_ORDER[-1]:
                    return None, [CODE_ARCHIVED_LOCKED]
                return None, [CODE_DUPLICATE_BATCH]

            entry: dict[str, Any] = {"id": store.next_id(MODULE)}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            # 完整保存业务字段：缺什么就显式落空，绝不静默丢弃用户提交的值。
            for field in BUSINESS_FIELDS:
                entry[field] = values.get(field)
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["abnormal_reason"] = []
            entry["snapshot_id"] = None
            store.rows(MODULE).append(entry)
        return self._with_availability(dict(entry)), []

    def _find_by_batch(self, batch_no: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("批次编号", "")) == batch_no:
                return row
        return None

    # -- 动作 -------------------------------------------------------------

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str, list[str]]:
        values = values or {}
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"落地批次 {entry_id} 不存在", []
        current = str(entry.get("status"))
        if current == STATUS_ORDER[-1]:
            return None, message_for(CODE_ARCHIVED_LOCKED), [CODE_ARCHIVED_LOCKED]

        if action == "发起落地":
            return self._start_landing(entry, current)
        if action == "确认已落地":
            return self._confirm_landing(entry, current, values)
        if action == "归档批次":
            return self._archive(entry, current)
        return None, f"动作「{action}」不属于原始落地可执行范围", []

    def _start_landing(
        self, entry: dict[str, Any], current: str
    ) -> tuple[dict[str, Any] | None, str, list[str]]:
        if current != "草稿":
            return None, message_for(CODE_ILLEGAL_TRANSITION), [CODE_ILLEGAL_TRANSITION]
        before = current
        with store.lock:
            try:
                with store.transaction(MODULE, "auditlog"):
                    entry["status"] = "落地中"
                    entry["pending"] = True
                    snapshot_service.write_audit(
                        operate_module=MODULE,
                        change_type="发起落地",
                        before={"status": before},
                        after={"status": "落地中"},
                        reason=f"批次 {entry.get('批次编号')} 发起落地",
                        snapshot_id=entry.get("snapshot_id"),
                    )
            except Exception:
                return None, "发起落地失败，已回滚且未改动登记快照", []
        return self._with_availability(dict(entry)), "落地批次已发起落地", []

    def _confirm_landing(
        self, entry: dict[str, Any], current: str, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str, list[str]]:
        if current != "落地中":
            # 已落地后再次确认属于重复确认；草稿/已归档由各自规则给出明确结论。
            code = CODE_ALREADY_COMMITTED if current == "已落地" else CODE_ILLEGAL_TRANSITION
            return None, message_for(code), [code]

        batch_no = str(entry["批次编号"])
        registered_row_count = self._registered_row_count(entry, values)
        snapshot_id = values.get("snapshot_id") or entry.get("snapshot_id")

        with store.lock:
            # 并发确认：锁内复查，已有 PASSED 快照则后到者一律拒收。
            if self.snapshots.passed_snapshot(batch_no) is not None:
                return None, message_for(CODE_ALREADY_COMMITTED), [CODE_ALREADY_COMMITTED]

            errors = self.snapshots.validate(
                batch_no=batch_no,
                values=values,
                registered_row_count=registered_row_count,
                self_snapshot_id=snapshot_id,
            )
            if errors:
                # 失败快照立即持久化，但批次状态、审计均不动——没有半套状态残留。
                self.snapshots.upsert_failed(
                    batch_no=batch_no,
                    source_system=str(entry.get("来源系统", "")),
                    values=values,
                    error_codes=errors,
                    snapshot_id=snapshot_id,
                    landing_id=int(entry["id"]),
                )
                entry["abnormal"] = True
                entry["abnormal_reason"] = list(errors)
                return None, "；".join(message_for(code) for code in errors), list(errors)

            snapshot, commit_errors = self.snapshots.commit_passed(
                batch_no=batch_no,
                source_system=str(entry.get("来源系统", "")),
                values=values,
                landing_id=int(entry["id"]),
                snapshot_id=snapshot_id,
            )
            if commit_errors:
                return None, "；".join(message_for(code) for code in commit_errors), list(commit_errors)

        return self._with_availability(dict(entry)), "校验通过，落地批次已确认并生成完整校验快照", []

    @staticmethod
    def _registered_row_count(
        entry: dict[str, Any], values: dict[str, Any]
    ) -> int | None:
        for candidate in (values.get("registered_row_count"), entry.get("原始行数")):
            if candidate in (None, ""):
                continue
            try:
                return int(candidate)
            except (TypeError, ValueError):
                continue
        return None

    def _archive(
        self, entry: dict[str, Any], current: str
    ) -> tuple[dict[str, Any] | None, str, list[str]]:
        # 草稿跨到归档、落地中直接归档都在这里被拒。
        if current != "已落地":
            return None, message_for(CODE_ILLEGAL_TRANSITION), [CODE_ILLEGAL_TRANSITION]
        snapshot = self.snapshots.passed_snapshot(str(entry["批次编号"]))
        if not self.snapshots.is_available(snapshot):
            return None, message_for(CODE_NO_SNAPSHOT), [CODE_NO_SNAPSHOT]

        before = current
        with store.lock:
            try:
                with store.transaction(MODULE, "auditlog"):
                    entry["status"] = "已归档"
                    entry["pending"] = False
                    snapshot_service.write_audit(
                        operate_module=MODULE,
                        change_type="归档批次",
                        before={"status": before},
                        after={"status": "已归档", "snapshot_id": snapshot["snapshot_id"]},
                        reason=f"批次 {entry.get('批次编号')} 按快照 {snapshot['snapshot_id']} 归档",
                        snapshot_id=snapshot["snapshot_id"],
                    )
            except Exception:
                return None, "归档失败，已回滚且未关闭原文件", []
        return self._with_availability(dict(entry)), "落地批次已归档，文件锁定不可再打开", []
