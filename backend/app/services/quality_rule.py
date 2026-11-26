"""质量规则库领域服务：管理主数据质量检查规则、阈值口径、严重级别与命中统计。

数据链路位置：质量治理层规则端，负责消费清洗产物执行检查，
把命中结果派发给质量报告模块。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.quality_result import QualityResultService
from app.store import store


def _report_service() -> QualityResultService:
    """取注册表里的质量报告服务：登记写入已收拢到报告端单点。"""
    from app.services import service_for

    service = service_for("quality_result")
    assert isinstance(service, QualityResultService)
    return service


class QualityRuleService(BaseModuleService):
    MODULE = "quality_rule"
    ENTITY = "质量规则"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["规则名称", "实体编码", "检查维度", "阈值口径", "严重级别"]
    REQUIRED_FIELDS = ["规则名称", "实体编码", "检查维度"]
    STATUS_ORDER = ["草稿", "已启用", "已停用"]
    ACTION_RULES = {"启用规则": "已启用", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    # ---- 业务扩展：质量规则命中与报告派发 ----

    def check_threshold(self, entry_id: int, value: float) -> dict[str, Any]:
        """按阈值口径判定命中，严重级别决定处置。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "质量规则不存在"}
        raw = str(entry.get("阈值口径", "0.9")).rstrip("%")
        try:
            threshold = float(raw)
        except ValueError:
            threshold = 0.9
        hit = value < threshold
        if hit:
            entry["今日命中"] = str(int(entry.get("今日命中", "0") or 0) + 1)
        return {"ok": True, "hit": hit, "threshold": threshold,
                "severity": entry.get("严重级别", "中"), "value": value}

    def run_rule_batch(self, entry_id: int, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """批量执行质量规则，输出命中的记录集合。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已启用":
            return {"ok": False, "message": "规则未启用", "hits": []}
        dimension = str(entry.get("检查维度", ""))
        hits = [r for r in rows if not str(r.get(dimension, "")).strip()]
        entry["今日命中"] = str(int(entry.get("今日命中", "0") or 0) + len(hits))
        return {"ok": True, "hits": hits, "checked": len(rows), "dimension": dimension}

    def dispatch_result_report(self, entry_id: int, hit_count: int, total: int) -> int:
        """派发质量报告生成任务，经统一入口登记到 quality_result。

        规则报告登记时已带实际命中（已生成、评分随批写入）；
        入口输入输出不变。同一规则同一轮重复派发时返回已登记的
        那份报告编号，命中不会被重新累计。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return 0
        receipt = _report_service().register_report_input(
            source="quality_rule",
            round_key=f"rule-{entry_id}",
            entity_code=str(entry.get("实体编码", "")),
            total=total,
            hits=hit_count,
            confirmed=True,
            batch_label=str(entry.get("规则名称", "")),
        )
        return int(receipt["report_id"]) if receipt.get("ok") else 0

    def consume_cleanse_output(self, entry_id: int) -> list[str]:
        """读取清洗模块输出的数据集作为检查输入。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return []
        return [r.get("规则名称", "") for r in store.rows("std_cleanse")
                if r.get("实体编码") == entry.get("实体编码") and r.get("status") == "已启用"]

    def disable_with_reason(self, entry_id: int, reason: str) -> str:
        """停用规则并登记停用原因，防止误启用。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return "质量规则不存在"
        entry["status"] = "已停用"
        entry["abnormal"] = True
        entry["停用原因"] = reason
        return f"规则 {entry.get('规则名称')} 已停用: {reason}"

    def align_severity_order(self, entry_id: int, min_severity: str) -> bool:
        """校准严重级别顺序，确保告警口径统一。"""
        order = {"低": 0, "中": 1, "高": 2}
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return False
        current = str(entry.get("严重级别", "中"))
        if order.get(current, 1) < order.get(min_severity, 1):
            entry["严重级别"] = min_severity
            return True
        return False
