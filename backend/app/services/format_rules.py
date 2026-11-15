"""转换器/解析模板共用的领域规则。

包括：
* 格式描述符（``json://raw?fields=a,b``）的解析；
* 字段映射与输入/输出格式的兼容性校验（启用转换器、生效模板共用同一套口径）；
* 生命周期冲突异常，路由层统一翻译成 409。
"""
from __future__ import annotations

import re
from typing import Any

_FORMAT_RE = re.compile(r"^\s*(?P<scheme>[a-zA-Z0-9_]+)://(?P<path>[^?]+?)(?:\?fields=(?P<fields>[^&]+))?\s*$")


class LifecycleError(Exception):
    """状态机或引用一致性被违反时抛出，由路由层转成 409。"""


def parse_format(descriptor: Any) -> dict[str, Any]:
    """把格式描述符拆成 scheme/path/fields；写不出标准结构的描述符直接拒绝。"""
    text = str(descriptor or "").strip()
    match = _FORMAT_RE.match(text)
    if not match:
        raise LifecycleError(
            f"格式描述符「{text or '空'}」不合法，需形如 json://raw_item?fields=a,b"
        )
    fields = [item.strip() for item in (match.group("fields") or "").split(",") if item.strip()]
    if not fields:
        raise LifecycleError(f"格式描述符「{text}」缺少 fields 字段清单，无法判定兼容性")
    return {
        "scheme": match.group("scheme").lower(),
        "path": match.group("path").strip(),
        "fields": fields,
        "raw": text,
    }


def normalize_mappings(mappings: Any) -> list[dict[str, str]]:
    """规整字段映射表，剔除空行并保证每行只有 source/target 两个键。"""
    if not isinstance(mappings, list) or not mappings:
        raise LifecycleError("字段映射表不能为空，至少保留一条 source→target 映射")
    normalized: list[dict[str, str]] = []
    seen_targets: set[str] = set()
    for index, item in enumerate(mappings, start=1):
        if not isinstance(item, dict):
            raise LifecycleError(f"字段映射第 {index} 行不是对象结构")
        source = str(item.get("source") or "").strip()
        target = str(item.get("target") or "").strip()
        if not source or not target:
            raise LifecycleError(f"字段映射第 {index} 行的 source/target 不允许为空")
        if target in seen_targets:
            raise LifecycleError(f"字段映射存在重复的目标字段「{target}」，重复提交会互相覆盖")
        seen_targets.add(target)
        normalized.append({"source": source, "target": target})
    return normalized


def check_mapping_compatibility(
    *,
    input_format: Any,
    output_format: Any,
    mappings: list[dict[str, str]],
) -> dict[str, Any]:
    """启用转换器前的自检：映射源字段必须来自输入格式，目标字段必须覆盖输出格式。

    返回解析后的格式描述符，供调用方冻结快照；不兼容时抛 LifecycleError，
    调用方处于事务中时整笔提交自动回滚。
    """
    source = parse_format(input_format)
    target = parse_format(output_format)
    input_fields = set(source["fields"])
    output_fields = set(target["fields"])
    mapping_sources = {row["source"] for row in mappings}
    mapping_targets = {row["target"] for row in mappings}

    unknown_sources = sorted(mapping_sources - input_fields)
    unknown_targets = sorted(mapping_targets - output_fields)
    missing_targets = sorted(output_fields - mapping_targets)
    if unknown_sources:
        raise LifecycleError(
            f"映射源字段 {unknown_sources} 不在输入格式 {source['fields']} 中，存在新增不兼容项"
        )
    if unknown_targets:
        raise LifecycleError(
            f"映射目标字段 {unknown_targets} 不在输出格式 {target['fields']} 中，存在新增不兼容项"
        )
    if missing_targets:
        raise LifecycleError(
            f"输出格式要求的字段 {missing_targets} 没有对应映射，输出格式无法被完整覆盖"
        )
    return {"source": source, "target": target}


def check_template_compatibility(
    *,
    template_output: Any,
    version_snapshot: dict[str, Any],
) -> None:
    """生效模板时校验：模板目标输出格式必须与所引用的已启用版本一致。

    方案（json/csv/parquet…）与字段清单都要对得上；路径不同只做提示不拦截，
    真正硬性不兼容的是序列化方案和字段集合。
    """
    template = parse_format(template_output)
    version = parse_format(version_snapshot.get("outputFormat"))
    if template["scheme"] != version["scheme"]:
        raise LifecycleError(
            f"模板输出方案「{template['scheme']}」与引用版本 {version_snapshot.get('versionCode')} "
            f"的输出方案「{version['scheme']}」不兼容，不允许生效"
        )
    if set(template["fields"]) != set(version["fields"]):
        raise LifecycleError(
            f"模板输出字段 {template['fields']} 与引用版本 {version_snapshot.get('versionCode')} "
            f"的输出字段 {version['fields']} 不一致，不允许生效"
        )


def idempotency_key(payload: dict[str, Any]) -> str:
    """映射提交的自然幂等键：同模板+同版本+同映射内容重复提交只落一条记录。"""
    import hashlib
    import json

    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
