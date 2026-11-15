"""转换器生命周期与解析模板引用一致性的回归验证（仅依赖标准库）。

运行：
    cd backend && .venv/bin/python -I tests/test_lifecycle.py

第一阶段直接驱动服务层，覆盖状态机、退役传播、事务回滚、幂等与只读快照；
第二阶段拉起真实 uvicorn 进程，走 HTTP 验证 409 拦截与端到端响应。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.formatter import FormatterService  # noqa: E402
from app.services.format_rules import LifecycleError  # noqa: E402
from app.services.parser import ParserService  # noqa: E402
from app.store import store  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}  {detail}")


def expect_error(label: str, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except LifecycleError as exc:
        check(label, True)
        print(f"        ↳ 拦截原因：{exc}")
        return
    except Exception as exc:  # noqa: BLE001 - 测试需要兜底捕获
        check(label, False, f"抛出的不是 LifecycleError 而是 {type(exc).__name__}: {exc}")
        return
    check(label, False, "预期的拦截没有发生")


def find_id(module: str, code: str, code_field: str) -> int:
    for row in store.rows(module):
        if row.get(code_field) == code:
            return int(row["id"])
    raise AssertionError(f"种子数据里找不到 {code}")


def seed_mutate(module: str, entry_id: int, **changes) -> None:
    """测试造数：在独立事务外直接改内存行，避免被下一笔动作的回滚带走。"""
    row = store.find(module, entry_id)
    assert row is not None
    row.update(changes)


GOOD_MAPPINGS = [
    {"source": "title", "target": "title"},
    {"source": "price", "target": "price"},
    {"source": "sku", "target": "sku"},
    {"source": "updated_at", "target": "updated_at"},
]
# 输出格式只声明 3 个字段时使用的完整映射
THREE_FIELD_MAPPINGS = GOOD_MAPPINGS[:3]
CSV_OUT_3 = "csv://item?fields=title,price,sku"
PARQUET_OUT = "parquet://article?fields=title,price,sku,updated_at"
CSV_OUT = "csv://article?fields=title,price,sku,updated_at"
JSON_IN = "json://raw_item?fields=title,price,sku,updated_at"


# ============================================================ 第一阶段：服务层
def phase_service() -> None:
    print("\n[阶段一] 服务层：状态机 / 退役传播 / 事务 / 幂等 / 快照")
    store.reset()
    fmt = FormatterService()
    pars = ParserService()

    f2 = find_id("formatter", "FORM-0002", "转换器编号")
    f3 = find_id("formatter", "FORM-0003", "转换器编号")
    p2 = find_id("parser", "PARS-0002", "模板编号")
    p3 = find_id("parser", "PARS-0003", "模板编号")

    print("-- 1. 转换器只能按 测试→启用→退役 推进，跳级/回退/终态全部拦截")
    expect_error("已启用状态执行「启用转换器」被拦截", fmt.run_action, f2, "启用转换器")
    expect_error("已启用状态跳回「提交转换测试」被拦截", fmt.run_action, f2, "提交转换测试")
    expect_error("未知动作被拦截", fmt.run_action, f2, "删除转换器")

    print("-- 2. 启用前的兼容性自检：坏映射不允许启用（草稿 FORM-0001）")
    f1 = find_id("formatter", "FORM-0001", "转换器编号")
    seed_mutate("formatter", f1, 字段映射表=[{"source": "ghost_field", "target": "title"}])
    fmt.run_action(f1, "提交转换测试")
    check("FORM-0001 先进入转换测试中", store.find("formatter", f1)["status"] == "转换测试中")
    expect_error("映射源字段不在输入格式中，启用被拦截", fmt.run_action, f1, "启用转换器")
    check("拦截后状态保持转换测试中（无半提交）", store.find("formatter", f1)["status"] == "转换测试中")
    check("拦截后没有冻结任何版本快照", store.find("formatter", f1)["当前版本快照"] is None)
    seed_mutate("formatter", f1, 字段映射表=THREE_FIELD_MAPPINGS, 输出格式=CSV_OUT_3)
    out, _ = fmt.run_action(f1, "启用转换器")
    check("修正映射后启用成功并冻结快照", out["status"] == "已启用" and isinstance(out["当前版本快照"], dict))
    check("版本表新增 FORM-0001@v1", fmt.find_version("FORM-0001@v1") is not None)

    print("-- 3. 退役传播：FORM-0002 退役，PARS-0002 与 PARS-0003 同时进入待复核")
    out, message = fmt.run_action(f2, "退役转换器")
    check("FORM-0002 状态为已退役", out["status"] == "已退役")
    check("退役时间被记录", bool(out["退役时间"]))
    t2, t3 = store.find("parser", p2), store.find("parser", p3)
    check("PARS-0002 进入待复核", t2["status"] == "待复核", t2["status"])
    check("PARS-0003 进入待复核", t3["status"] == "待复核", t3["status"])
    check("待复核模板标记 pending/abnormal", t2["pending"] is True and t2["abnormal"] is True)
    print(f"        ↳ {message}")

    print("-- 4. 一个退役版本只有一条迁移结论，两个模板看到的是同一个结论编号")
    conclusions = fmt.list_migration_conclusions()
    rel = [c for c in conclusions if c["retiredVersionCode"] == "FORM-0002@v1"]
    check("FORM-0002@v1 只有一条迁移结论", len(rel) == 1, str(rel))
    check("结论覆盖两个受影响模板", rel and sorted(rel[0]["affectedTemplates"]) == ["PARS-0002", "PARS-0003"])
    check("两个模板引用同一结论编号", t2["迁移结论"] == t3["迁移结论"] == rel[0]["conclusionCode"])

    print("-- 5. 退役为终态：退役后任何动作都被拦截，不会再产生输出")
    expect_error("退役后再执行退役被拦截", fmt.run_action, f2, "退役转换器")
    expect_error("退役后尝试启用被拦截（不能直接复活）", fmt.run_action, f2, "启用转换器")

    print("-- 6. 退役后保留最后一次完整输入输出格式样本，待迁移模板只读可见")
    snap = store.find("formatter", f2)["当前版本快照"]
    check("快照保留输入格式与字段", snap["inputFormat"] == JSON_IN and snap["inputFields"] == ["title", "price", "sku", "updated_at"])
    check("快照保留输出格式与字段", snap["outputFormat"] == PARQUET_OUT)
    check("快照保留完整字段映射", snap["mappings"] == GOOD_MAPPINGS)
    check("快照标记为已退役且带退役时间", snap["status"] == "已退役" and bool(snap["retiredAt"]))
    check("PARS-0002 保留最后输出格式样本", t2["最后输出格式样本"]["sourceVersion"] == "FORM-0002@v1")
    check("版本表中 FORM-0002@v1 同步置退役", fmt.find_version("FORM-0002@v1")["status"] == "已退役")

    print("-- 7. 模板只能引用已启用版本：引用退役版本生效被拦截")
    expect_error("引用已退役 FORM-0002@v1 生效被拦截",
                 pars.run_action, p2, "生效模板",
                 {"formatterCode": "FORM-0002", "versionNo": "v1",
                  "targetOutputFormat": PARQUET_OUT, "mappings": GOOD_MAPPINGS})
    check("拦截后 PARS-0002 仍是待复核（无半提交）", store.find("parser", p2)["status"] == "待复核")

    print("-- 8. 兼容拦截：迁移到 FORM-0003（仅 csv 输出）与模板保留的 parquet 目标不兼容")
    expect_error("parquet→csv 方案不一致，迁移被拦截",
                 pars.run_action, p2, "迁移转换器引用",
                 {"formatterCode": "FORM-0003", "versionNo": "v1",
                  "targetOutputFormat": PARQUET_OUT, "mappings": GOOD_MAPPINGS})
    bad_fields = "parquet://article?fields=title,price"
    expect_error("字段清单不一致，迁移被拦截",
                 pars.run_action, p2, "迁移转换器引用",
                 {"formatterCode": "FORM-0003", "versionNo": "v1",
                  "targetOutputFormat": bad_fields, "mappings": GOOD_MAPPINGS[:2]})
    check("拦截后没有新增任何映射记录", len(store.rows("parser_mappings")) == 0)
    check("拦截后 PARS-0002 仍是待复核", store.find("parser", p2)["status"] == "待复核")

    print("-- 9. 建一条 parquet 后继启用版本，两个模板先后迁移，结论收敛为唯一且最终完成")
    succ, msg = fmt.create_successor(f2)
    check("后继转换器基于退役快照生成草稿", succ["status"] == "草稿" and succ["转换器编号"] == "FORM-0002-NEXT")
    check("退役件登记后继编号", store.find("formatter", f2)["后继转换器编号"] == "FORM-0002-NEXT")
    succ["输出格式"] = PARQUET_OUT
    succ_id = int(succ["id"])
    fmt.run_action(succ_id, "提交转换测试")
    fmt.run_action(succ_id, "启用转换器")
    check("后继版本已冻结并启用", fmt.find_version("FORM-0002-NEXT@v1")["status"] == "已启用")
    expect_error("重复登记后继被拦截", fmt.create_successor, f2)

    print("-- 10. 重复提交幂等：自然键去重 + 并发双击只允许落一条映射")
    # 10a 直接验证幂等键：同模板同版本同映射内容两次写入返回同一条
    version = fmt.find_version("FORM-0002-NEXT@v1")
    entry_p2 = store.find("parser", p2)
    row_a = pars._upsert_mapping(entry=entry_p2, version=version, mappings=GOOD_MAPPINGS,
                                 target_output=PARQUET_OUT, status_after="已生效")
    rows_before = len(store.rows("parser_mappings"))
    row_b = pars._upsert_mapping(entry=entry_p2, version=version, mappings=GOOD_MAPPINGS,
                                 target_output=PARQUET_OUT, status_after="已生效")
    check("相同内容重复写入返回同一条映射", row_a["id"] == row_b["id"])
    check("映射表没有新增记录", len(store.rows("parser_mappings")) == rows_before)
    entry_p3 = store.find("parser", p3)
    row_c = pars._upsert_mapping(entry=entry_p3, version=version, mappings=GOOD_MAPPINGS,
                                 target_output=PARQUET_OUT, status_after="已生效")
    check("不同模板的同内容映射互不覆盖（键含模板维度）", row_c["id"] != row_a["id"])
    # 清掉手工写入，回到干净的待复核起点再走完整流程
    store.rows("parser_mappings").clear()

    # 10b 并发双击：两个线程同时迁移同一个待复核模板，最终只允许一条映射
    import threading
    errors: list[str] = []
    barrier = threading.Barrier(2)

    def concurrent_migrate() -> None:
        barrier.wait()
        try:
            pars.run_action(
                p2, "迁移转换器引用",
                {"formatterCode": "FORM-0002-NEXT", "versionNo": "v1",
                 "targetOutputFormat": PARQUET_OUT, "mappings": GOOD_MAPPINGS})
        except LifecycleError as exc:
            errors.append(str(exc))

    t1 = threading.Thread(target=concurrent_migrate)
    t2 = threading.Thread(target=concurrent_migrate)
    t1.start(); t2.start(); t1.join(); t2.join()
    p2_mappings = [r for r in store.rows("parser_mappings") if r["parserEntryId"] == p2]
    check("并发双击后 PARS-0002 只有一条映射", len(p2_mappings) == 1, str(p2_mappings))
    check("PARS-0002 最终为已生效", store.find("parser", p2)["status"] == "已生效")
    check("落败的一次请求拿到明确拦截说明（不会静默丢失）", len(errors) <= 1)

    out = store.find("parser", p2)
    check("PARS-0002 引用切换到后继版本", out["引用转换器编号"] == "FORM-0002-NEXT")
    rel0 = [c for c in fmt.list_migration_conclusions() if c["retiredVersionCode"] == "FORM-0002@v1"][0]
    check("首个模板迁完，结论仍待迁移且只剩一个受影响模板",
          rel0["status"] == "待迁移" and rel0["affectedTemplates"] == ["PARS-0003"])
    pars.run_action(
        p3, "迁移转换器引用",
        {"formatterCode": "FORM-0002-NEXT", "versionNo": "v1",
         "targetOutputFormat": PARQUET_OUT, "mappings": GOOD_MAPPINGS})
    rel0 = [c for c in fmt.list_migration_conclusions() if c["retiredVersionCode"] == "FORM-0002@v1"][0]
    check("两个模板都迁完后结论置为已完成", rel0["status"] == "已完成")
    check("结论始终只有一条", len([c for c in fmt.list_migration_conclusions()
                                    if c["retiredVersionCode"] == "FORM-0002@v1"]) == 1)

    print("-- 11. 串行重复提交：已生效模板再次迁移被状态机拦下，映射数保持不变")
    mappings_before = list(store.rows("parser_mappings"))
    expect_error("已生效模板不能重复迁移",
                 pars.run_action, p2, "迁移转换器引用",
                 {"formatterCode": "FORM-0002-NEXT", "versionNo": "v1",
                  "targetOutputFormat": PARQUET_OUT, "mappings": GOOD_MAPPINGS})
    check("拦截后映射表一条不多", store.rows("parser_mappings") == mappings_before)

    print("-- 12. 映射保存与模板状态同事务：映射非法时两侧都不保留新值")
    p1 = find_id("parser", "PARS-0001", "模板编号")
    pars.run_action(p1, "提交模板验证")
    mappings_before = list(store.rows("parser_mappings"))
    expect_error("含空 target 的映射被拦截",
                 pars.run_action, p1, "生效模板",
                 {"formatterCode": "FORM-0003", "versionNo": "v1",
                  "targetOutputFormat": CSV_OUT,
                  "mappings": [{"source": "title", "target": ""}]})
    check("回滚后映射表一条都没多", store.rows("parser_mappings") == mappings_before)
    check("回滚后 PARS-0001 仍停在模板验证中", store.find("parser", p1)["status"] == "模板验证中")
    expect_error("重复 target 的映射被拦截（防止互相覆盖）",
                 pars.run_action, p1, "生效模板",
                 {"formatterCode": "FORM-0003", "versionNo": "v1",
                  "targetOutputFormat": CSV_OUT,
                  "mappings": [{"source": "title", "target": "title"},
                               {"source": "price", "target": "title"}]})
    out, _, m1 = pars.run_action(
        p1, "生效模板",
        {"formatterCode": "FORM-0003", "versionNo": "v1",
         "targetOutputFormat": CSV_OUT, "mappings": GOOD_MAPPINGS})
    check("合法提交后模板生效且映射落库", out["status"] == "已生效" and m1 is not None)

    print("-- 13. 重新启用前确认无新增不兼容项：后继启用/重复启用约束")
    expect_error("已启用版本重复启用被拦截", fmt.run_action, f3, "启用转换器")
    check("FORM-0003 保持已启用", store.find("formatter", f3)["status"] == "已启用")


# ============================================================ 第二阶段：HTTP 端到端
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(method: str, url: str, payload: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    # 查询串里的中文状态需要百分号编码，否则 urllib 会按 ascii 编码报错
    if "?" in url:
        path, query = url.split("?", 1)
        url = path + "?" + urllib.parse.quote(query, safe="=&")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def phase_http() -> None:
    print("\n[阶段二] HTTP 端到端：真实 uvicorn 进程下的状态码与响应体")
    port = _free_port()
    backend = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen(
        [str(backend / ".venv" / "bin" / "uvicorn"), "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=backend, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            try:
                status, body = _request("GET", f"{base}/api/health")
                if status == 200 and body.get("ok"):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError("后端进程未在预期时间内启动")

        f2 = find_id("formatter", "FORM-0002", "转换器编号")
        f3 = find_id("formatter", "FORM-0003", "转换器编号")
        p2 = find_id("parser", "PARS-0002", "模板编号")

        print("-- 14. 非法生命周期动作返回 409 且带可读 detail")
        status, body = _request("POST", f"{base}/api/formatter/{f2}/actions",
                                {"values": {"action": "启用转换器"}})
        check("已启用件再启用返回 409", status == 409, str(body))
        check("409 响应包含中文原因", "只能执行" in body.get("detail", ""))

        print("-- 15. 退役传播走 HTTP 完整链路")
        status, body = _request("POST", f"{base}/api/formatter/{f2}/actions",
                                {"values": {"action": "退役转换器"}})
        check("退役成功 200", status == 200 and body["ok"] and body["entry"]["status"] == "已退役")
        status, body = _request("GET", f"{base}/api/parser/{p2}")
        check("详情接口显示 PARS-0002 待复核", body["status"] == "待复核")
        status, body = _request("GET", f"{base}/api/parser?status=待复核")
        check("列表接口同样只显示待复核结论（列表/详情一致）",
              status == 200 and {i["模板编号"] for i in body["items"]} == {"PARS-0002", "PARS-0003"})
        status, body = _request("GET", f"{base}/api/formatter/migration-conclusions")
        check("迁移结论接口只有一条 FORM-0002@v1 结论",
              len([i for i in body["items"] if i["retiredVersionCode"] == "FORM-0002@v1"]) == 1)

        print("-- 16. 模板生效兼容拦截走 HTTP（409，且无映射残留）")
        # 模板保留 parquet 目标，FORM-0003 只输出 csv：方案不一致必须拦截
        status, body = _request("POST", f"{base}/api/parser/{p2}/actions", {
            "values": {"action": "迁移转换器引用", "formatterCode": "FORM-0003",
                       "versionNo": "v1", "targetOutputFormat": PARQUET_OUT, "mappings": GOOD_MAPPINGS}})
        check("不兼容迁移返回 409", status == 409 and "不兼容" in body["detail"])
        status, body = _request("GET", f"{base}/api/parser/mappings")
        check("拦截后映射表仍为空（无结果丢失/旧值残留）", body["items"] == [])

        print("-- 17. 兼容迁移成功 + 重复提交幂等走 HTTP")
        # HTTP 进程是独立内存，需要通过 HTTP 建后继并推进
        status, created = _request("POST", f"{base}/api/formatter", {"values": {
            "转换器编号": "FORM-0002-NEXT", "输入格式": JSON_IN, "输出格式": PARQUET_OUT,
            "版本号": "v1", "字段映射表": GOOD_MAPPINGS}})
        check("HTTP 登记后继转换器成功", status == 200 and created["ok"])
        next_id = created["entry"]["id"]
        _request("POST", f"{base}/api/formatter/{next_id}/actions",
                 {"values": {"action": "提交转换测试"}})
        status, enabled = _request("POST", f"{base}/api/formatter/{next_id}/actions",
                                   {"values": {"action": "启用转换器"}})
        check("HTTP 后继启用并冻结快照", status == 200 and enabled["entry"]["当前版本快照"]["versionCode"] == "FORM-0002-NEXT@v1")
        payload = {"values": {"action": "迁移转换器引用", "formatterCode": "FORM-0002-NEXT",
                              "versionNo": "v1", "targetOutputFormat": PARQUET_OUT,
                              "mappings": GOOD_MAPPINGS}}
        status, first = _request("POST", f"{base}/api/parser/{p2}/actions", payload)
        check("首次迁移生效成功", status == 200 and first["entry"]["status"] == "已生效")
        status, second = _request("POST", f"{base}/api/parser/{p2}/actions", payload)
        check("重复提交被状态机明确拒绝（409，不静默、不重复落库）",
              status == 409 and "只能执行" in second["detail"])
        status, body = _request("GET", f"{base}/api/parser/mappings?parser_id={p2}")
        check("PARS-0002 名下只有一条映射记录", len(body["items"]) == 1)
        status, detail = _request("GET", f"{base}/api/parser/{p2}")
        check("再次查询详情与列表结论一致，无重复显示",
              detail["status"] == "已生效" and detail["引用转换器编号"] == "FORM-0002-NEXT")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    phase_service()
    phase_http()
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
