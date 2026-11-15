"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

跨模块的一致性（例如转换器退役时批量挂起解析模板）依赖 ``transaction``：
事务内任一步骤抛出异常，整批表数据回滚到事务开始前，不会留下半条映射。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tables: dict[str, list[dict[str, Any]]] = self._seed_tables()

    def _seed_tables(self) -> dict[str, list[dict[str, Any]]]:
        return {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}

    def reset(self) -> None:
        """恢复出厂示例数据，主要给测试用例提供干净起点。"""
        with self._lock:
            self._tables = self._seed_tables()

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """单进程内的事务边界：异常即整体回滚，正常退出才提交。

        内存仓库没有真正的 WAL，这里用深拷贝快照实现回滚；所有跨表联动
        （退役传播、映射与模板状态同进同退）都必须包在同一个事务里。
        """
        with self._lock:
            snapshot = copy.deepcopy(self._tables)
            try:
                yield
            except Exception:
                self._tables = snapshot
                raise

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
