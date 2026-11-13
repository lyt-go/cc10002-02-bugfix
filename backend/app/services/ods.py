"""贴源层业务规则。

贴源准入直接复用落地侧的共享校验快照：

* 只有 ``PASSED`` 快照对应的落地批次才能被贴源表读取/准入，
  草稿、落地中、校验失败的批次在贴源结果里不可见；
* 落地批次列表与贴源表详情通过同一个 ``SnapshotService.availability``
  判断可用版本，冲突时一律以已提交的完整快照为准；
* 准入与确认上线在同一事务内写贴源关系和审计，失败两处同时退回，
  不留半套审计，也不改动已持久化的原始登记。
"""
from __future__ import annotations

from typing import Any

from app.services.snapshot import (
    CODE_MESSAGES,
    SnapshotService,
    snapshot_service,
)
from app.services.landing import message_for as landing_message
from app.store import store

MODULE = "ods"
REQUIRED_FIELDS = ["表编号", "表名称", "来源系统"]
STATUS_ORDER = ["草稿", "建表审批中", "已上线", "已下线"]
ACTION_RULES = {"提交建表审批": "建表审批中", "确认上线": "已上线", "下线表": "已下线"}
NEGATIVE_ACTIONS = ["下线表"]

CODE_NO_BATCH = "BATCH_NOT_FOUND"
CODE_BATCH_NOT_READABLE = "BATCH_NOT_READABLE"

ODS_MESSAGES = {
    CODE_NO_BATCH: "关联的落地批次不存在",
    CODE_BATCH_NOT_READABLE: "贴源只读取校验通过批次：该落地批次尚无 PASSED 校验快照",
}


def _message(code: str) -> str:
    return ODS_MESSAGES.get(code) or CODE_MESSAGES.get(code) or landing_message(code)


class OdsService:
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
            rows = [row for row in rows if keyword in str(row.get("表编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
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
        """与落地列表同源的可用性判断，并带出关联快照的版本信息。"""
        batch_no = entry.get("关联批次编号")
        if batch_no:
            entry["可用性判断"] = self.snapshots.availability(str(batch_no))
            snapshot = self.snapshots.passed_snapshot(str(batch_no))
            if snapshot:
                entry["准入快照"] = {
                    "snapshot_id": snapshot["snapshot_id"],
                    "digest_type": snapshot.get("digest_type"),
                    "digest_value": snapshot.get("digest_value"),
                    "byte_size": snapshot.get("byte_size"),
                    "row_count": snapshot.get("row_count"),
                    "completed_at": snapshot.get("completed_at"),
                }
        else:
            entry["可用性判断"] = {"available": False, "snapshot_id": None, "reason": "未关联落地批次"}
        return entry

    def readable_batches(self) -> list[dict[str, Any]]:
        """贴源结果视图：只列出校验通过、可被读取的落地批次。"""
        result = []
        for landing in store.rows("landing"):
            batch_no = str(landing.get("批次编号", ""))
            snapshot = self.snapshots.passed_snapshot(batch_no)
            if not self.snapshots.is_available(snapshot):
                continue
            result.append({
                "批次编号": batch_no,
                "来源系统": landing.get("来源系统"),
                "落地路径": landing.get("落地路径"),
                "文件格式": snapshot.get("file_format") or landing.get("文件格式"),
                "原始行数": snapshot.get("row_count"),
                "snapshot_id": snapshot["snapshot_id"],
                "completed_at": snapshot.get("completed_at"),
                "批次状态": landing.get("status"),
            })
        return result

    # -- 登记 -------------------------------------------------------------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry: dict[str, Any] = {"id": store.next_id(MODULE)}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["关联批次编号"] = values.get("关联批次编号")
        entry["snapshot_id"] = None
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return self._with_availability(dict(entry)), []

    # -- 贴源准入：两个入口之一，与落地确认共享同一份快照 ----------------

    def admit(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str, list[str]]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"贴源表 {entry_id} 不存在", []
        batch_no = str(values.get("批次编号") or entry.get("关联批次编号") or "").strip()
        landing = self._find_landing(batch_no)
        if landing is None:
            return None, _message(CODE_NO_BATCH), [CODE_NO_BATCH]

        snapshot = self.snapshots.passed_snapshot(batch_no)
        if not self.snapshots.is_available(snapshot):
            return None, _message(CODE_BATCH_NOT_READABLE), [CODE_BATCH_NOT_READABLE]

        # 同一张贴源表重复准入同一快照：幂等返回，不重复写审计。
        if entry.get("snapshot_id") == snapshot["snapshot_id"] and entry.get("status") == "已上线":
            return self._with_availability(dict(entry)), "该快照版本已准入，无需重复操作", []

        before_status = entry.get("status")
        with store.lock:
            try:
                with store.transaction(MODULE, "auditlog"):
                    entry["关联批次编号"] = batch_no
                    entry["snapshot_id"] = snapshot["snapshot_id"]
                    entry["status"] = "已上线"
                    entry["pending"] = False
                    entry["abnormal"] = False
                    self.snapshots.write_audit(
                        operate_module=MODULE,
                        change_type="贴源准入",
                        before={"status": before_status, "snapshot_id": entry.get("snapshot_id")},
                        after={"status": "已上线", "snapshot_id": snapshot["snapshot_id"]},
                        reason=(
                            f"贴源表 {entry.get('表编号')} 准入批次 {batch_no}，"
                            f"以完整快照 {snapshot['snapshot_id']} 为准"
                        ),
                        snapshot_id=snapshot["snapshot_id"],
                    )
            except Exception:
                return None, "贴源准入失败，已回滚且未留下半套审计", []
        return self._with_availability(dict(entry)), "贴源表已按校验通过快照准入上线", []

    # -- 原有状态流转 -----------------------------------------------------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"贴源表 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于贴源层可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        # 确认上线必须挂着一份 PASSED 快照；贴源结果只读取校验通过批次。
        if action == "确认上线":
            batch_no = entry.get("关联批次编号")
            if not batch_no or not self.snapshots.is_available(
                self.snapshots.passed_snapshot(str(batch_no))
            ):
                return None, _message(CODE_BATCH_NOT_READABLE)

        before_status = entry.get("status")
        with store.lock:
            try:
                with store.transaction(MODULE, "auditlog"):
                    entry["status"] = target
                    entry["pending"] = target != STATUS_ORDER[-1]
                    entry["abnormal"] = action in NEGATIVE_ACTIONS
                    self.snapshots.write_audit(
                        operate_module=MODULE,
                        change_type=action,
                        before={"status": before_status},
                        after={"status": target, "snapshot_id": entry.get("snapshot_id")},
                        reason=f"贴源表 {entry.get('表编号')} 执行动作 {action}",
                        snapshot_id=entry.get("snapshot_id"),
                    )
            except Exception:
                return None, f"动作「{action}」执行失败，已回滚"
        return self._with_availability(dict(entry)), f"贴源表已{action}"

    @staticmethod
    def _find_landing(batch_no: str) -> dict[str, Any] | None:
        if not batch_no:
            return None
        for row in store.rows("landing"):
            if str(row.get("批次编号", "")) == batch_no:
                return row
        return None
