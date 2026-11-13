"""贴源层接口：维护贴源表，贴源准入只接受带 PASSED 校验快照的落地批次。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.ods import OdsService

router = APIRouter(prefix="/api/ods", tags=["贴源层"])

service = OdsService()

LIST_FIELDS = [
    "表编号", "表名称", "来源系统", "关联批次编号", "snapshot_id",
    "可用性判断", "准入快照", "原始字段数", "增量字段", "全量快照策略",
    "每日增量行", "最近加载时间",
]
STATUSES = ["草稿", "建表审批中", "已上线", "已下线"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按表编号检索"),
    status: str | None = Query(default=None, description="草稿、建表审批中、已上线、已下线"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按表编号与状态过滤贴源层列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/readable-batches", response_model=dict)
def readable_batches() -> dict[str, Any]:
    """贴源结果视图：只读取校验通过（PASSED 快照）的落地批次。"""
    items = service.readable_batches()
    return {"total": len(items), "items": items}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出贴源层清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "ods", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条贴源表明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"贴源表 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条贴源表，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="贴源表已登记", entry=entry)


@router.post("/{entry_id}/admit", response_model=ActionResult)
def admit(entry_id: int, payload: EntryPayload) -> ActionResult:
    """贴源准入：把落地批次的完整校验快照接入贴源表；未通过校验的批次被拒。"""
    entry, message, codes = service.admit(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message, codes=codes)
    return ActionResult(ok=True, message=message, entry=entry, codes=codes)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条贴源表执行提交建表审批、确认上线、下线表；上线需有 PASSED 快照。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
