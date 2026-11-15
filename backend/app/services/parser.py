"""字段解析器业务规则：模板生命周期、转换器引用校验与字段映射原子提交。

模板只能引用「已启用」的转换器版本；生效时校验目标输出格式与版本快照兼容；
字段映射保存与模板状态切换共用同一事务，任一侧失败整笔回滚；
重复提交按自然幂等键返回同一条映射，不会产生互相覆盖的新结果。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.format_rules import (
    LifecycleError,
    check_template_compatibility,
    idempotency_key,
    normalize_mappings,
)
from app.services.formatter import MIGRATION_MODULE, VERSION_MODULE
from app.store import store

MODULE = "parser"
FORMATTER_MODULE = "formatter"
MAPPING_MODULE = "parser_mappings"
REQUIRED_FIELDS = ["模板编号", "目标字段数", "XPath表达式"]
STATUS_ORDER = ["草稿", "模板验证中", "已生效", "待复核", "已废弃"]
# 严格下推：每个状态只暴露一个合法动作；待复核只能先迁移到新版本再重新生效
NEXT_ACTION: dict[str, str] = {
    "草稿": "提交模板验证",
    "模板验证中": "生效模板",
    "已生效": "废弃模板",
    "待复核": "迁移转换器引用",
}
ACTION_RULES = {"提交模板验证": "模板验证中", "生效模板": "已生效", "废弃模板": "已废弃"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ParserService:
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
            rows = [row for row in rows if keyword in str(row.get("模板编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        entry = {"id": store.next_id(MODULE)}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry.update({
            "status": STATUS_ORDER[0],
            "pending": True,
            "abnormal": False,
            "正则匹配式": values.get("正则匹配式") or "",
            "类型转换规则": values.get("类型转换规则") or "",
            "默认填充值": values.get("默认填充值") or "",
            "多值分隔符": values.get("多值分隔符") or "",
            "嵌套结构深度": values.get("嵌套结构深度") or "",
            "引用转换器编号": "",
            "引用版本号": "",
            "目标输出格式": "",
            "最后输出格式样本": None,
            "迁移结论": "",
        })
        store.rows(MODULE).append(entry)
        return entry, []

    # ------------------------------------------------------------------ 动作
    def run_action(self, entry_id: int, action: str, values: dict[str, Any] | None = None):
        values = values or {}
        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                raise LifecycleError(f"解析模板 {entry_id} 不存在或已归档")
            if action not in ACTION_RULES and action != "迁移转换器引用":
                raise LifecycleError(f"动作「{action}」不属于字段解析器可执行范围")

            current = str(entry.get("status"))
            allowed = NEXT_ACTION.get(current)
            if allowed is None:
                raise LifecycleError(f"模板处于终态「{current}」，不允许再执行任何动作")
            if action != allowed:
                raise LifecycleError(
                    f"模板当前为「{current}」，只能执行「{allowed}」，不允许跳级或回退"
                )

            if action == "提交模板验证":
                entry["status"] = "模板验证中"
                entry["pending"] = True
                return entry, "模板已提交验证", None

            if action == "生效模板":
                mapping = self._activate(entry, values)
                return entry, "模板已生效，字段映射与状态已在同一事务提交", mapping

            if action == "迁移转换器引用":
                mapping = self._migrate(entry, values)
                return entry, "模板已迁移到新的已启用转换器版本并重新生效", mapping

            # 废弃模板
            entry["status"] = "已废弃"
            entry["pending"] = False
            return entry, "模板已废弃", None

    # ------------------------------------------------------------- 生效/迁移
    def _resolve_enabled_version(self, formatter_code: str, version_no: str) -> dict[str, Any]:
        version_code = f"{formatter_code}@{version_no}"
        version = next(
            (row for row in store.rows(VERSION_MODULE) if row.get("versionCode") == version_code),
            None,
        )
        if version is None:
            raise LifecycleError(
                f"转换器版本 {version_code} 不存在，模板只能引用已冻结的启用版本"
            )
        if version.get("status") != "已启用":
            raise LifecycleError(
                f"转换器版本 {version_code} 已{version.get('status')}，模板只能引用「已启用」版本"
            )
        return version

    def _activate(self, entry: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        formatter_code = str(values.get("formatterCode") or entry.get("引用转换器编号") or "").strip()
        version_no = str(values.get("versionNo") or entry.get("引用版本号") or "v1").strip()
        target_output = str(values.get("targetOutputFormat") or entry.get("目标输出格式") or "").strip()
        if not formatter_code:
            raise LifecycleError("生效前必须指定要引用的转换器编号")
        if not target_output:
            raise LifecycleError("生效前必须填写模板目标输出格式")
        version = self._resolve_enabled_version(formatter_code, version_no)
        check_template_compatibility(template_output=target_output, version_snapshot=version)

        mappings = normalize_mappings(values.get("mappings"))
        # 映射与模板状态在同一事务内提交：上面任一步抛错都会整体回滚
        mapping_row = self._upsert_mapping(
            entry=entry,
            version=version,
            mappings=mappings,
            target_output=target_output,
            status_after="已生效",
        )
        entry["status"] = "已生效"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["引用转换器编号"] = formatter_code
        entry["引用版本号"] = version_no
        entry["目标输出格式"] = target_output
        entry["最后输出格式样本"] = {
            "format": version.get("outputFormat"),
            "fields": list(version.get("outputFields") or []),
            "sourceVersion": version.get("versionCode"),
        }
        entry["迁移结论"] = ""
        return mapping_row

    def _migrate(self, entry: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        formatter_code = str(values.get("formatterCode") or "").strip()
        version_no = str(values.get("versionNo") or "v1").strip()
        target_output = str(values.get("targetOutputFormat") or "").strip()
        if not formatter_code:
            raise LifecycleError("迁移必须指定新的转换器编号")
        if not target_output:
            raise LifecycleError("迁移必须确认模板目标输出格式")
        version = self._resolve_enabled_version(formatter_code, version_no)
        check_template_compatibility(template_output=target_output, version_snapshot=version)
        mappings = normalize_mappings(values.get("mappings"))

        old_conclusion_code = str(entry.get("迁移结论") or "")
        mapping_row = self._upsert_mapping(
            entry=entry,
            version=version,
            mappings=mappings,
            target_output=target_output,
            status_after="已生效",
        )
        entry["status"] = "已生效"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["引用转换器编号"] = formatter_code
        entry["引用版本号"] = version_no
        entry["目标输出格式"] = target_output
        entry["最后输出格式样本"] = {
            "format": version.get("outputFormat"),
            "fields": list(version.get("outputFields") or []),
            "sourceVersion": version.get("versionCode"),
        }
        entry["迁移结论"] = ""
        self._refresh_conclusion(old_conclusion_code, entry)
        return mapping_row

    def _upsert_mapping(
        self,
        *,
        entry: dict[str, Any],
        version: dict[str, Any],
        mappings: list[dict[str, str]],
        target_output: str,
        status_after: str,
    ) -> dict[str, Any]:
        """按自然幂等键落映射：重复提交返回原记录，绝不生成互相覆盖的第二条。"""
        key_payload = {
            "parserId": int(entry["id"]),
            "versionCode": version["versionCode"],
            "targetOutputFormat": target_output,
            "mappings": mappings,
        }
        digest = idempotency_key(key_payload)
        table = store.rows(MAPPING_MODULE)
        for row in table:
            if row.get("idempotencyKey") == digest:
                row["lastSubmittedAt"] = _now()
                row["templateStatus"] = status_after
                return row
        row = {
            "id": store.next_id(MAPPING_MODULE),
            "idempotencyKey": digest,
            "parserEntryId": int(entry["id"]),
            "模板编号": entry.get("模板编号"),
            "versionCode": version["versionCode"],
            "转换器编号": version.get("转换器编号"),
            "版本号": version.get("版本号"),
            "targetOutputFormat": target_output,
            "mappings": mappings,
            "templateStatus": status_after,
            "submittedAt": _now(),
            "lastSubmittedAt": _now(),
        }
        table.append(row)
        return row

    def _refresh_conclusion(self, conclusion_code: str, migrated_entry: dict[str, Any]) -> None:
        """两个模板引用同一退役版本时只有一条结论：全部迁完才置为已完成。"""
        if not conclusion_code:
            return
        for conclusion in store.rows(MIGRATION_MODULE):
            if conclusion.get("conclusionCode") != conclusion_code:
                continue
            affected = [code for code in conclusion.get("affectedTemplates") or []
                        if code != migrated_entry.get("模板编号")]
            still_pending = any(
                row.get("迁移结论") == conclusion_code and row.get("status") == "待复核"
                for row in store.rows(MODULE)
            )
            conclusion["affectedTemplates"] = affected
            conclusion["updatedAt"] = _now()
            if not still_pending:
                conclusion["status"] = "已完成"
                conclusion["recommendedVersionCode"] = str(migrated_entry.get("引用转换器编号", "")) + "@" + str(migrated_entry.get("引用版本号", ""))
            return

    def list_mappings(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows(MAPPING_MODULE)
        if entry_id is not None:
            rows = [row for row in rows if int(row.get("parserEntryId", 0)) == entry_id]
        return rows
