"""图谱构建领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class GraphBuildService(BaseModuleService):
    MODULE = "graph_build"
    ENTITY = "构建任务"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["任务编号", "数据范围", "图类型", "实体数量", "关系数量", "构建模式", "增量标记", "构建时间"]
    REQUIRED_FIELDS = ["任务编号", "数据范围", "图类型"]
    STATUS_ORDER = ["待执行", "构建中", "中止待确认", "构建完成", "已中止", "构建异常"]
    ACTION_RULES = {
        "启动构建": "构建中",
        "中止构建": "中止待确认",
        "确认中止": "已中止",
        "确认完成": "构建完成",
        "标记异常": "构建异常",
    }
    NEGATIVE_ACTIONS = []
    TERMINAL_STATUSES = ["构建完成", "已中止", "构建异常"]

    # 终态落地后只允许「启动构建」开启新一代构建，其余冲突通知一律拒绝。
    RESTART_ACTION = "启动构建"
    # 合法迁移表：键为当前状态，值为该状态接受的动作集合。
    TRANSITIONS: dict[str, set[str]] = {
        "待执行": {"启动构建"},
        "构建中": {"中止构建", "确认完成", "标记异常"},
        "中止待确认": {"确认中止"},
        "构建完成": {"启动构建"},
        "已中止": {"启动构建"},
        "构建异常": {"启动构建"},
    }
    ACTION_MESSAGES = {
        "启动构建": "构建任务已启动构建",
        "中止构建": "中止请求已登记，构建任务进入中止待确认",
        "确认中止": "构建任务已确认中止",
        "确认完成": "构建任务已确认完成",
        "标记异常": "构建任务已标记异常",
    }

    # ===== 状态机：终态优先级 + 幂等通知 =====
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        current = str(entry.get("status", ""))
        # 幂等：同一通知重复提交只生效一次，不重复派发跨模块构建输入。
        if current == target:
            return entry, f"{self.ENTITY}已处于「{current}」，重复的「{action}」通知不再生效"
        # 终态优先级：已落地的终态不被冲突通知覆盖，只允许重新启动构建。
        if current in self.TERMINAL_STATUSES and action != self.RESTART_ACTION:
            return None, f"{self.ENTITY}已处于终态「{current}」，「{action}」通知被拒绝"
        # 中止流程优先：中止待确认期间只接受确认中止，完成通知不得抢先落地。
        if current == "中止待确认" and action != "确认中止":
            return None, f"{self.ENTITY}正在中止待确认，「{action}」通知被拒绝，请先确认中止"
        if action not in self.TRANSITIONS.get(current, set()):
            return None, f"当前状态「{current}」不允许执行「{action}」"

        entry["status"] = target
        entry["pending"] = target not in self.TERMINAL_STATUSES
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS
        message = self.ACTION_MESSAGES.get(action, f"{self.ENTITY}已{action}")

        # 只有真正完成的构建才派发索引构建输入与自动推导血缘；
        # 两者在同一次流转中写入同一代结果，保证索引清单与数据血缘的构建代同步。
        if action == "确认完成":
            generation = int(entry.get("构建代", 0) or 0) + 1
            entry["构建代"] = generation
            idx_msg = self._push_to_graph_index(entry, generation)
            message += "；" + idx_msg
            lineage_msg = self._push_to_data_lineage(entry, generation)
            message += "；" + lineage_msg
        return entry, message

    # ===== 跨模块数据流实现 =====
    def _push_to_graph_index(self, entry: dict[str, Any], generation: int) -> str:
        # 链路 L3: graph_build → graph_index（构建完成后更新索引）
        idx_rows = store.rows("graph_index")
        next_id = max((int(r.get("id", 0)) for r in idx_rows), default=0) + 1
        idx_entry: dict[str, Any] = {
            "id": next_id,
            "status": "待构建",
            "pending": True,
            "abnormal": False,
            "索引编号": f"GRAP-IDX-{next_id:04d}",
            "目标属性": str(entry.get("图类型", "")),
            "索引类型": "graph-entity-fulltext",
            "覆盖实体": str(entry.get("实体数量", "0")),
            "存储体积": "estimated-128MB",
            "构建耗时": "",
            "刷新周期": "on-build-complete",
            "最近重建": str(entry.get("构建时间", "2026-11-18")),
            "构建任务": str(entry.get("任务编号", "")),
            "构建代": generation,
        }
        idx_rows.append(idx_entry)
        return f"已更新图谱索引 {idx_entry['索引编号']}（构建代{generation}）"

    def _push_to_data_lineage(self, entry: dict[str, Any], generation: int) -> str:
        # 链路 L4: graph_build → data_lineage（构建完成后触发血缘追踪）
        lineage_rows = store.rows("data_lineage")
        next_id = max((int(r.get("id", 0)) for r in lineage_rows), default=0) + 1
        lin_entry: dict[str, Any] = {
            "id": next_id,
            "status": "自动推导",
            "pending": True,
            "abnormal": False,
            "链路编号": f"DATA-LINE-{next_id:04d}",
            "上游实体": str(entry.get("数据范围", "")),
            "下游实体": str(entry.get("图类型", "")),
            "转换描述": f"graph_build_id={entry.get('id',0)};构建代={generation}",
            "关联字段": str(entry.get("任务编号", "")),
            "影响层级": "graph-level",
            "链路类型": "auto-derived",
            "最后更新": str(entry.get("构建时间", "2026-11-18")),
            "构建任务": str(entry.get("任务编号", "")),
            "构建代": generation,
        }
        lineage_rows.append(lin_entry)
        return f"已触发血缘追踪 {lin_entry['链路编号']}（构建代{generation}）"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["graph_index", "data_lineage"]
        return base
