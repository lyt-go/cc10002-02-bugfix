"""校验快照：原始落地与贴源准入两个入口共享的完整校验快照。

一份快照把登记时刻的业务字段和校验时刻的实测结果固化在一起，
落地批次列表与贴源表详情都通过同一份快照判断"这个版本到底能不能用"：

* 快照 ``status == PASSED`` 才是可用版本，贴源只准入这类批次；
* 校验失败只留下 ``FAILED`` 快照作为按 ``snapshot_id`` 恢复的锚点，
  不推进批次状态、不写审计、不产生贴源准入；
* 提交动作在同一个事务里改快照、落地批次、贴源关系和审计，
  任一步失败整体回滚，不留半套审计。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.store import store

SNAPSHOT_MODULE = "snapshot"
AUDIT_MODULE = "auditlog"

STATUS_PASSED = "PASSED"
STATUS_FAILED = "FAILED"

# 可区分的校验结论：空文件、压缩包多文件、同名重传、分片冲突各自有独立码，
# 调用方可以据此给用户不同的处置提示。
CODE_EMPTY_FILE = "EMPTY_FILE"
CODE_MULTI_FILE_ARCHIVE = "MULTI_FILE_ARCHIVE"
CODE_DIGEST_MISSING = "DIGEST_MISSING"
CODE_ROW_COUNT_MISMATCH = "ROW_COUNT_MISMATCH"
CODE_SHARD_CONFLICT = "SHARD_CONFLICT"
CODE_DUPLICATE_FILE = "DUPLICATE_FILE"
CODE_ALREADY_COMMITTED = "SNAPSHOT_ALREADY_COMMITTED"
CODE_SNAPSHOT_NOT_FOUND = "SNAPSHOT_NOT_FOUND"
CODE_COMMIT_ROLLED_BACK = "COMMIT_ROLLED_BACK"

CODE_MESSAGES = {
    CODE_EMPTY_FILE: "文件大小为 0，空文件不允许进入下一阶段",
    CODE_MULTI_FILE_ARCHIVE: "压缩包内包含多个文件，单批次只接受单文件落地",
    CODE_DIGEST_MISSING: "缺少摘要算法或摘要值，无法校验文件完整性",
    CODE_ROW_COUNT_MISMATCH: "行数缺失或与登记/清单声明的行数不符",
    CODE_SHARD_CONFLICT: "摘要相同但文件大小不同，判定为分片冲突",
    CODE_DUPLICATE_FILE: "同名文件已按相同摘要与大小登记，属于重复传输",
    CODE_ALREADY_COMMITTED: "批次已提交完整校验快照，并发确认只接受首份结果",
    CODE_SNAPSHOT_NOT_FOUND: "指定的 snapshot_id 不存在或不属于该批次",
    CODE_COMMIT_ROLLED_BACK: "快照提交过程中发生异常，已整体回滚",
}

SNAPSHOT_FIELDS = (
    "digest_type",
    "digest_value",
    "byte_size",
    "row_count",
    "file_format",
    "received_at",
    "source_manifest",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_manifest(value: Any) -> dict[str, Any]:
    """source_manifest 允许传 dict 或 JSON 字符串，统一成 dict。"""
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def manifest_files(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    files = manifest.get("files")
    return list(files) if isinstance(files, list) else []


class SnapshotService:
    """共享校验快照服务：纯校验、快照持久化、提交事务与可用性查询。"""

    # -- 查询 -------------------------------------------------------------

    def list_for_batch(self, batch_no: str) -> list[dict[str, Any]]:
        rows = [
            row for row in store.rows(SNAPSHOT_MODULE)
            if row.get("batch_no") == batch_no
        ]
        return sorted(rows, key=lambda row: int(row.get("id", 0)))

    def get_by_snapshot_id(self, snapshot_id: str) -> dict[str, Any] | None:
        for row in store.rows(SNAPSHOT_MODULE):
            if row.get("snapshot_id") == snapshot_id:
                return row
        return None

    def passed_snapshot(self, batch_no: str) -> dict[str, Any] | None:
        """批次已提交的完整校验快照；冲突时以这一份为准。"""
        for row in self.list_for_batch(batch_no):
            if row.get("status") == STATUS_PASSED:
                return row
        return None

    def is_available(self, snapshot: dict[str, Any] | None) -> bool:
        return bool(snapshot and snapshot.get("status") == STATUS_PASSED)

    def availability(self, batch_no: str) -> dict[str, Any]:
        """落地列表与贴源详情共用的可用性判断。"""
        snapshot = self.passed_snapshot(batch_no)
        if self.is_available(snapshot):
            return {
                "available": True,
                "snapshot_id": snapshot["snapshot_id"],
                "reason": "校验通过",
            }
        latest = self.list_for_batch(batch_no)[-1:]
        reason = "尚无校验快照"
        if latest and latest[0].get("error_codes"):
            reason = "、".join(latest[0]["error_codes"])
        return {"available": False, "snapshot_id": None, "reason": reason}

    # -- 纯校验：不写任何数据，方便单测每种异常得到可区分结论 -------------

    def validate(
        self,
        *,
        batch_no: str,
        values: dict[str, Any],
        registered_row_count: int | None = None,
        self_snapshot_id: str | None = None,
    ) -> list[str]:
        errors: list[str] = []

        byte_size = _to_int(values.get("byte_size"))
        if byte_size is None or byte_size <= 0:
            errors.append(CODE_EMPTY_FILE)

        manifest = normalize_manifest(values.get("source_manifest"))
        if len(manifest_files(manifest)) > 1:
            errors.append(CODE_MULTI_FILE_ARCHIVE)

        digest_type = str(values.get("digest_type") or "").strip()
        digest_value = str(values.get("digest_value") or "").strip()
        if not digest_type or not digest_value:
            errors.append(CODE_DIGEST_MISSING)

        row_count = _to_int(values.get("row_count"))
        manifest_row_count = _to_int(manifest.get("row_count"))
        if row_count is None or row_count <= 0:
            errors.append(CODE_ROW_COUNT_MISMATCH)
        else:
            if registered_row_count is not None and row_count != registered_row_count:
                errors.append(CODE_ROW_COUNT_MISMATCH)
            if manifest_row_count is not None and row_count != manifest_row_count:
                errors.append(CODE_ROW_COUNT_MISMATCH)

        # 完整性基础校验没过，就不再给分片/重传结论，避免一次报互相矛盾的码。
        if errors:
            return self._dedupe(errors)

        for prior in self.list_for_batch(batch_no):
            if self_snapshot_id and prior.get("snapshot_id") == self_snapshot_id:
                continue
            if prior.get("digest_value") != digest_value:
                continue
            prior_size = _to_int(prior.get("byte_size"))
            if prior_size == byte_size:
                errors.append(CODE_DUPLICATE_FILE)
            else:
                errors.append(CODE_SHARD_CONFLICT)
            break
        return self._dedupe(errors)

    @staticmethod
    def _dedupe(codes: list[str]) -> list[str]:
        """同一类问题（如登记行数与清单行数同时不符）只保留一个错误码。"""
        seen: set[str] = set()
        result: list[str] = []
        for code in codes:
            if code not in seen:
                seen.add(code)
                result.append(code)
        return result

    # -- 持久化 -----------------------------------------------------------

    def _new_snapshot_id(self, batch_no: str) -> str:
        seq = len(self.list_for_batch(batch_no)) + 1
        return f"SNAP-{batch_no}-{seq:03d}"

    def upsert_failed(
        self,
        *,
        batch_no: str,
        source_system: str,
        values: dict[str, Any],
        error_codes: list[str],
        snapshot_id: str | None = None,
        landing_id: int | None = None,
    ) -> dict[str, Any]:
        """落一张失败快照并立即提交：它是恢复锚点，不属于可回滚的半套审计。"""
        with store.lock:
            existing = None
            if snapshot_id:
                candidate = self.get_by_snapshot_id(snapshot_id)
                if candidate and candidate.get("batch_no") == batch_no:
                    existing = candidate
            row = existing or {
                "id": store.next_id(SNAPSHOT_MODULE),
                "snapshot_id": snapshot_id or self._new_snapshot_id(batch_no),
                "batch_no": batch_no,
                "source_system": source_system,
            }
            row.update({
                "landing_id": landing_id,
                "status": STATUS_FAILED,
                "error_codes": list(error_codes),
                "completed_at": None,
                "created_at": row.get("created_at") or now_iso(),
                **{field: values.get(field) for field in SNAPSHOT_FIELDS},
            })
            row["source_manifest"] = normalize_manifest(values.get("source_manifest"))
            if existing is None:
                store.rows(SNAPSHOT_MODULE).append(row)
            return dict(row)

    def commit_passed(
        self,
        *,
        batch_no: str,
        source_system: str,
        values: dict[str, Any],
        landing_id: int,
        snapshot_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """校验通过后在一个事务里提交快照并推进落地批次、写审计。

        返回 ``(快照, 错误码)``。并发下只有首份完整快照能提交成功，
        后到的确认拿到 ``SNAPSHOT_ALREADY_COMMITTED``，两边状态都不变。
        """
        with store.lock:
            if self.passed_snapshot(batch_no) is not None:
                return None, [CODE_ALREADY_COMMITTED]

            existing = None
            if snapshot_id:
                candidate = self.get_by_snapshot_id(snapshot_id)
                if candidate and candidate.get("batch_no") == batch_no:
                    existing = candidate
                else:
                    return None, [CODE_SNAPSHOT_NOT_FOUND]

            target_id = existing["id"] if existing else store.next_id(SNAPSHOT_MODULE)
            target_snapshot_id = (
                existing["snapshot_id"] if existing else (snapshot_id or self._new_snapshot_id(batch_no))
            )
            try:
                with store.transaction(SNAPSHOT_MODULE, "landing", AUDIT_MODULE):
                    if existing is not None:
                        row = existing
                    else:
                        row = {
                            "id": target_id,
                            "snapshot_id": target_snapshot_id,
                            "batch_no": batch_no,
                            "source_system": source_system,
                            "created_at": now_iso(),
                        }
                        store.rows(SNAPSHOT_MODULE).append(row)
                    row.update({
                        "landing_id": landing_id,
                        "status": STATUS_PASSED,
                        "error_codes": [],
                        "completed_at": now_iso(),
                        **{field: values.get(field) for field in SNAPSHOT_FIELDS},
                    })
                    row["source_manifest"] = normalize_manifest(values.get("source_manifest"))

                    landing = store.find("landing", landing_id)
                    if landing is None:
                        raise RuntimeError("落地批次在提交过程中消失")
                    before_status = landing.get("status")
                    landing["status"] = "已落地"
                    landing["pending"] = False
                    landing["abnormal"] = False
                    landing["abnormal_reason"] = []
                    landing["snapshot_id"] = row["snapshot_id"]

                    self._write_audit(
                        operate_module="landing",
                        change_type="校验快照提交",
                        before={"status": before_status},
                        after={"status": "已落地", "snapshot_id": row["snapshot_id"]},
                        reason=f"批次 {batch_no} 校验通过，快照 {row['snapshot_id']} 提交",
                        snapshot_id=row["snapshot_id"],
                    )
                return dict(row), []
            except Exception:
                # 事务已把四个模块回滚；补一张失败快照保留现场，供按 snapshot_id 恢复。
                self.upsert_failed(
                    batch_no=batch_no,
                    source_system=source_system,
                    values=values,
                    error_codes=[CODE_COMMIT_ROLLED_BACK],
                    snapshot_id=target_snapshot_id,
                    landing_id=landing_id,
                )
                return None, [CODE_COMMIT_ROLLED_BACK]

    def write_audit(
        self,
        *,
        operate_module: str,
        change_type: str,
        before: dict[str, Any],
        after: dict[str, Any],
        reason: str,
        snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        """在调用方事务内追加一条审计；随事务一起提交或回滚。"""
        return self._write_audit(
            operate_module=operate_module,
            change_type=change_type,
            before=before,
            after=after,
            reason=reason,
            snapshot_id=snapshot_id,
        )

    def _write_audit(
        self,
        *,
        operate_module: str,
        change_type: str,
        before: dict[str, Any],
        after: dict[str, Any],
        reason: str,
        snapshot_id: str | None,
    ) -> dict[str, Any]:
        rows = store.rows(AUDIT_MODULE)
        entry = {
            "id": store.next_id(AUDIT_MODULE),
            "status": "已记录",
            "pending": False,
            "abnormal": False,
            "记录编号": f"AUDI-{len(rows) + 1:04d}",
            "操作表名": operate_module,
            "变更类型": change_type,
            "变更前值": json.dumps(before, ensure_ascii=False),
            "变更后值": json.dumps(after, ensure_ascii=False),
            "操作人": "system",
            "操作时间": now_iso(),
            "变更原因": reason,
            "snapshot_id": snapshot_id,
        }
        rows.append(entry)
        return entry


snapshot_service = SnapshotService()
