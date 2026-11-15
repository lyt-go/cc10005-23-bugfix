"""格式转换业务规则：严格状态机、版本快照与退役传播。

转换器只能按 草稿 → 转换测试中 → 已启用 → 已退役 单向推进，退役为终态。
启用时冻结一份不可变版本快照；退役时在同一事务内把引用该版本的解析模板
统一置为「待复核」，并保证一个退役版本只产生一条迁移结论。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.format_rules import (
    LifecycleError,
    check_mapping_compatibility,
    normalize_mappings,
)
from app.store import store

MODULE = "formatter"
VERSION_MODULE = "formatter_versions"
MIGRATION_MODULE = "migration_conclusions"
PARSER_MODULE = "parser"
MAPPING_MODULE = "parser_mappings"
REQUIRED_FIELDS = ["转换器编号", "输入格式", "输出格式"]
STATUS_ORDER = ["草稿", "转换测试中", "已启用", "已退役"]
RETIRED = STATUS_ORDER[-1]
# 每个状态只允许执行唯一的下推动作，杜绝跳级与回退
NEXT_ACTION: dict[str, str] = {
    "草稿": "提交转换测试",
    "转换测试中": "启用转换器",
    "已启用": "退役转换器",
}
ACTION_RULES = {"提交转换测试": "转换测试中", "启用转换器": "已启用", "退役转换器": "已退役"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _freeze_snapshot(entry: dict[str, Any], frozen_at: str) -> dict[str, Any]:
    """从转换器当前配置生成不可变版本快照，退役后模板只读看到的就是这份内容。"""
    mappings = normalize_mappings(entry.get("字段映射表"))
    parsed = check_mapping_compatibility(
        input_format=entry.get("输入格式"),
        output_format=entry.get("输出格式"),
        mappings=mappings,
    )
    code = f"{entry['转换器编号']}@{entry.get('版本号') or 'v1'}"
    return {
        "versionCode": code,
        "formatterEntryId": int(entry["id"]),
        "转换器编号": entry["转换器编号"],
        "版本号": entry.get("版本号") or "v1",
        "status": "已启用",
        "inputFormat": parsed["source"]["raw"],
        "inputFields": parsed["source"]["fields"],
        "outputFormat": parsed["target"]["raw"],
        "outputFields": parsed["target"]["fields"],
        "mappings": mappings,
        "frozenAt": frozen_at,
        "retiredAt": None,
    }


class FormatterService:
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
            rows = [row for row in rows if keyword in str(row.get("转换器编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def find_by_code(self, code: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if row.get("转换器编号") == code:
                return row
        return None

    def find_version(self, version_code: str) -> dict[str, Any] | None:
        for row in store.rows(VERSION_MODULE):
            if row.get("versionCode") == version_code:
                return row
        return None

    def list_versions(self, *, status: str | None = None) -> list[dict[str, Any]]:
        """版本快照清单；模板引用选择器默认只取「已启用」版本。"""
        rows = store.rows(VERSION_MODULE)
        if status:
            rows = [row for row in rows if row.get("status") == status]
        return rows

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        entry = {"id": store.next_id(MODULE)}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry.update({
            "版本号": str(values.get("版本号") or "v1").strip(),
            "字段映射表": values.get("字段映射表") or [],
            "日期格式模板": values.get("日期格式模板") or "",
            "数值精度": values.get("数值精度") or "",
            "嵌套展平层数": values.get("嵌套展平层数") or "",
            "空值输出策略": values.get("空值输出策略") or "",
            "status": STATUS_ORDER[0],
            "pending": True,
            "abnormal": False,
            "当前版本快照": None,
            "退役时间": None,
            "后继转换器编号": "",
        })
        store.rows(MODULE).append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """执行生命周期动作；所有跨表联动都在同一个事务里，失败整体回滚。"""
        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                raise LifecycleError(f"转换器 {entry_id} 不存在或已归档")
            if action not in ACTION_RULES:
                raise LifecycleError(f"动作「{action}」不属于格式转换可执行范围")

            current = str(entry.get("status"))
            allowed = NEXT_ACTION.get(current)
            if allowed is None:
                raise LifecycleError(f"转换器处于终态「{current}」，不允许再执行任何动作")
            if action != allowed:
                raise LifecycleError(
                    f"转换器当前为「{current}」，只能执行「{allowed}」，"
                    f"不允许跳级或回退到「{ACTION_RULES[action]}」"
                )

            if action == "提交转换测试":
                entry["status"] = "转换测试中"
                entry["pending"] = True
                return entry, "转换器已提交转换测试"

            if action == "启用转换器":
                self._enable(entry)
                return entry, "转换器已启用并冻结版本快照"

            # 退役转换器
            self._retire(entry)
            return entry, self._retire_message(entry)

    def _enable(self, entry: dict[str, Any]) -> None:
        snapshot = _freeze_snapshot(entry, _now())
        code = str(snapshot["versionCode"])
        existing = self.find_version(code)
        if existing is not None and existing.get("status") == "已启用":
            raise LifecycleError(f"版本 {code} 已启用，重复启用不会生成新快照")
        # 重新启用前确认字段映射与目标格式没有新增不兼容项：校验已在 _freeze_snapshot 完成
        if existing is not None:
            existing.update(snapshot)
            stored = existing
        else:
            stored = {"id": store.next_id(VERSION_MODULE), **snapshot}
            store.rows(VERSION_MODULE).append(stored)
        entry["status"] = "已启用"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["当前版本快照"] = dict(stored)

    def _retire(self, entry: dict[str, Any]) -> None:
        snapshot = entry.get("当前版本快照")
        if not isinstance(snapshot, dict):
            raise LifecycleError("转换器尚未冻结启用版本快照，不能退役")
        code = str(snapshot["versionCode"])
        affected = [
            row for row in store.rows(PARSER_MODULE)
            if row.get("引用版本号") and f"{row.get('引用转换器编号')}@{row.get('引用版本号')}" == code
        ]
        now = _now()
        # 1) 版本快照置退役（保留最后一次完整输入输出格式样本）
        for version in store.rows(VERSION_MODULE):
            if version.get("versionCode") == code:
                version["status"] = "已退役"
                version["retiredAt"] = now
        # 2) 受影响模板进入待复核，保留最后输出格式样本，迁移结论全表唯一
        affected_codes = [str(row.get("模板编号")) for row in affected]
        conclusion = self._upsert_migration_conclusion(code, entry, affected_codes, now)
        for row in affected:
            row["status"] = "待复核"
            row["pending"] = True
            row["abnormal"] = True
            row["迁移结论"] = conclusion["conclusionCode"]
            if not row.get("最后输出格式样本"):
                row["最后输出格式样本"] = {
                    "format": snapshot.get("outputFormat"),
                    "fields": list(snapshot.get("outputFields") or []),
                    "sourceVersion": code,
                }
        entry["status"] = RETIRED
        entry["pending"] = False
        entry["abnormal"] = False
        entry["退役时间"] = now
        entry["当前版本快照"] = dict(snapshot) | {"status": "已退役", "retiredAt": now}

    def _upsert_migration_conclusion(
        self,
        version_code: str,
        entry: dict[str, Any],
        affected_codes: list[str],
        now: str,
    ) -> dict[str, Any]:
        """一个退役版本只落一条结论；重复退役/重复传播拿到的都是同一条。"""
        table = store.rows(MIGRATION_MODULE)
        for row in table:
            if row.get("retiredVersionCode") == version_code:
                merged = sorted(set(row.get("affectedTemplates") or []) | set(affected_codes))
                row["affectedTemplates"] = merged
                row["updatedAt"] = now
                return row
        conclusion = {
            "id": store.next_id(MIGRATION_MODULE),
            "conclusionCode": f"MIG-{version_code.replace('@', '-')}",
            "retiredVersionCode": version_code,
            "转换器编号": entry.get("转换器编号"),
            "版本号": entry.get("版本号"),
            "status": "待迁移",
            "affectedTemplates": affected_codes,
            "recommendedVersionCode": "",
            "lastOutputFormat": entry.get("当前版本快照", {}).get("outputFormat"),
            "createdAt": now,
            "updatedAt": now,
        }
        table.append(conclusion)
        return conclusion

    def _retire_message(self, entry: dict[str, Any]) -> str:
        code = f"{entry['转换器编号']}@{entry.get('版本号') or 'v1'}"
        affected = [
            row for row in store.rows(PARSER_MODULE)
            if f"{row.get('引用转换器编号')}@{row.get('引用版本号')}" == code
        ]
        if not affected:
            return "转换器已退役，当前没有解析模板引用该版本"
        names = "、".join(str(row.get("模板编号")) for row in affected)
        return f"转换器已退役，引用模板 {names} 已进入待复核，迁移结论已统一生成"

    def create_successor(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """退役转换器不能直接复活：基于保留的最后快照登记一条后继草稿。"""
        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                raise LifecycleError(f"转换器 {entry_id} 不存在或已归档")
            if entry.get("status") != RETIRED:
                raise LifecycleError("只有已退役的转换器才需要登记后继版本")
            base_code = str(entry["转换器编号"])
            successor_code = f"{base_code}-NEXT"
            if self.find_by_code(successor_code) is not None:
                raise LifecycleError(f"后继转换器 {successor_code} 已存在，请直接在其基础上推进测试")
            snapshot = entry.get("当前版本快照") or {}
            successor, missing = self.create_entry({
                "转换器编号": successor_code,
                "输入格式": snapshot.get("inputFormat") or entry.get("输入格式"),
                "输出格式": snapshot.get("outputFormat") or entry.get("输出格式"),
                "版本号": "v1",
                "字段映射表": snapshot.get("mappings") or entry.get("字段映射表"),
                "日期格式模板": entry.get("日期格式模板"),
                "数值精度": entry.get("数值精度"),
                "嵌套展平层数": entry.get("嵌套展平层数"),
                "空值输出策略": entry.get("空值输出策略"),
            })
            if missing:
                raise LifecycleError(f"后继转换器缺少必填字段：{'、'.join(missing)}")
            entry["后继转换器编号"] = successor_code
            return successor, f"已基于退役快照登记后继转换器 {successor_code}（草稿），需重新走测试与启用"

    def list_migration_conclusions(self) -> list[dict[str, Any]]:
        return store.rows(MIGRATION_MODULE)
