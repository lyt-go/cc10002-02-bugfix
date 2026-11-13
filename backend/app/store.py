"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

在示例内存实现之上补了三件落地链路必需的能力：

* ``lock``：进程级互斥锁，同一批次并发确认时业务层可以在锁内做"先查后写"；
* ``transaction``：快照、贴源准入、审计这类跨模块写入走同一个事务，
  失败时按模块整体回滚，不会留下半套审计或半条准入关系；
* ``reset``：测试之间把仓库恢复到种子状态，避免用例互相污染。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        """把所有模块恢复成种子数据；测试与异常恢复共用这一入口。"""
        with self._lock:
            self._tables = {
                name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
            }

    @property
    def lock(self) -> threading.RLock:
        return self._lock

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
        """生成模块内稳定自增主键；空表从 1 开始。"""
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    @contextmanager
    def transaction(self, *modules: str) -> Iterator[None]:
        """跨模块事务：上下文内对给定模块表的改动，异常时整体回滚。

        只回滚传入的模块——早先已经提交的原始登记不在事务模块里，
        因此校验失败回滚不会删掉已持久化的登记快照。
        """
        with self._lock:
            backup = {
                module: [dict(row) for row in self.rows(module)]
                for module in modules
            }
            try:
                yield
            except Exception:
                for module, rows in backup.items():
                    self._tables[module] = [dict(row) for row in rows]
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
