"""字段解析器接口：维护解析模板，覆盖验证、生效、废弃与退役版本迁移。

生效/迁移请求同时携带字段映射，服务层在同一事务内落映射并切换状态，
任一侧失败整体回滚；重复提交命中幂等键，返回同一条映射记录。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.format_rules import LifecycleError
from app.services.parser import ParserService

router = APIRouter(prefix="/api/parser", tags=["字段解析器"])

service = ParserService()

LIST_FIELDS = ["模板编号", "目标字段数", "XPath表达式", "正则匹配式", "类型转换规则", "默认填充值", "多值分隔符", "嵌套结构深度", "status", "引用转换器编号", "引用版本号", "目标输出格式", "最后输出格式样本", "迁移结论"]
STATUSES = ["草稿", "模板验证中", "已生效", "待复核", "已废弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按模板编号检索"),
    status: str | None = Query(default=None, description="草稿、模板验证中、已生效、待复核、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模板编号与状态过滤字段解析器列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/mappings")
def list_mappings(parser_id: int | None = Query(default=None, description="可选：只看某个模板的映射")) -> dict[str, Any]:
    """字段映射提交记录，供详情面板只读查看。"""
    return {"items": service.list_mappings(parser_id)}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条解析模板明细；待复核模板会带回最后输出格式样本与迁移结论编号。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"解析模板 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条解析模板，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="解析模板已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条解析模板执行动作；生效/迁移会在同一事务内提交字段映射与模板状态。"""
    action = str(payload.values.get("action") or "").strip()
    try:
        entry, message, mapping = service.run_action(entry_id, action, payload.values)
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    result = ActionResult(ok=True, message=message, entry=entry)
    if mapping is not None:
        # 复用 entry 通道带回幂等映射，前端可直接确认“重复提交没有新增记录”
        result.message = f"{message}（映射记录 #{mapping['id']}）"
    return result


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出字段解析器清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "parser", "total": total, "items": items}
