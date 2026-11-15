"""格式转换接口：维护转换器，覆盖提交转换测试、启用转换器、退役转换器等动作。

状态机冲突与引用一致性错误统一抛 409，前端靠 message 展示可读原因，
不会再出现“点了没反应、列表详情结论不一致”。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.format_rules import LifecycleError
from app.services.formatter import FormatterService

router = APIRouter(prefix="/api/formatter", tags=["格式转换"])

service = FormatterService()

LIST_FIELDS = ["转换器编号", "版本号", "输入格式", "输出格式", "字段映射表", "日期格式模板", "数值精度", "嵌套展平层数", "空值输出策略", "status", "当前版本快照", "退役时间", "后继转换器编号"]
STATUSES = ["草稿", "转换测试中", "已启用", "已退役"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按转换器编号检索"),
    status: str | None = Query(default=None, description="草稿、转换测试中、已启用、已退役"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按转换器编号与状态过滤格式转换列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/migration-conclusions")
def migration_conclusions() -> dict[str, Any]:
    """退役迁移结论清单：一个退役版本只有一条确定结论。"""
    return {"items": service.list_migration_conclusions()}


@router.get("/versions")
def list_versions(status: str | None = Query(default=None, description="默认全部，传 已启用 只取可被模板引用的版本")) -> dict[str, Any]:
    """转换器版本快照清单；解析模板只能引用其中状态为「已启用」的版本。"""
    return {"items": service.list_versions(status=status)}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条转换器明细；退役转换器同时返回只读的最后版本快照。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"转换器 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条转换器，缺字段时说明原因而不是静默丢弃。"""
    try:
        entry, missing = service.create_entry(payload.values)
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="转换器已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条转换器执行生命周期动作；跳级、回退、不兼容都会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    try:
        entry, message = service.run_action(entry_id, action)
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/successor", response_model=ActionResult)
def create_successor(entry_id: int) -> ActionResult:
    """退役转换器不允许直接复活：基于最后快照登记后继草稿，重新走测试/启用。"""
    try:
        successor, message = service.create_successor(entry_id)
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ActionResult(ok=True, message=message, entry=successor)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出格式转换清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "formatter", "total": total, "items": items}
