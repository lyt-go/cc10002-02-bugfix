"""原始落地接口：维护落地批次，覆盖登记、发起落地、校验确认、归档等动作。

动作接口把整份 ``values`` 透传给业务层，确认已落地时携带校验快照字段
（digest_type/digest_value/byte_size/row_count/source_manifest/snapshot_id）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.landing import LandingService
from app.services.snapshot import snapshot_service

router = APIRouter(prefix="/api/landing", tags=["原始落地"])

service = LandingService()

LIST_FIELDS = [
    "批次编号", "来源系统", "落地路径", "文件格式", "接收时间",
    "原始行数", "摘要算法", "摘要值", "文件大小", "批次状态", "可用性判断",
]
STATUSES = ["草稿", "落地中", "已落地", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按批次编号检索"),
    status: str | None = Query(default=None, description="草稿、落地中、已落地、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按批次编号与状态过滤原始落地列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/readable", response_model=dict)
def readable_batches() -> dict[str, Any]:
    """贴源读取视图：只返回带 PASSED 校验快照、可被下游读取的批次。"""
    from app.services.ods import OdsService

    items = OdsService().readable_batches()
    return {"total": len(items), "items": items}


@router.get("/snapshots/{snapshot_id}", response_model=dict)
def get_snapshot(snapshot_id: str) -> dict[str, Any]:
    """按 snapshot_id 读取校验快照，供失败后恢复续传。"""
    snapshot = snapshot_service.get_by_snapshot_id(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"校验快照 {snapshot_id} 不存在")
    return dict(snapshot)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出原始落地清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "landing", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条落地批次明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"落地批次 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条落地批次，完整保存业务字段；缺字段或编号冲突时说明原因。"""
    entry, codes = service.create_entry(payload.values)
    if codes:
        from app.services.landing import message_for

        return ActionResult(
            ok=False,
            message="；".join(message_for(code) for code in codes),
            codes=codes,
        )
    return ActionResult(ok=True, message="落地批次已登记，原始登记已完整持久化", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行发起落地、确认已落地、归档批次；校验不过或状态非法时带结构化码拒绝。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message, codes = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message, codes=codes)
    return ActionResult(ok=True, message=message, entry=entry, codes=codes)
