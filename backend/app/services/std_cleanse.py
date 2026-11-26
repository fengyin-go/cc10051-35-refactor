"""标准化清洗领域服务：管理主数据记录的清洗规则、阶段编排、异常处置与检测派发。

数据链路位置：主数据加工层第一环，负责消费抽取作业产物，
把清洗后的数据集派发给重复检测与质量检查。
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


class StdCleanseService(BaseModuleService):
    MODULE = "std_cleanse"
    ENTITY = "清洗规则"
    OPERATION_TERM = "清洗"
    LIST_FIELDS = ["规则名称", "实体编码", "清洗阶段", "规则表达式", "异常处理"]
    REQUIRED_FIELDS = ["规则名称", "实体编码", "清洗阶段"]
    STATUS_ORDER = ["草稿", "已启用", "已停用"]
    ACTION_RULES = {"启用规则": "已启用", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    # ---- 业务扩展：标准化清洗与检测派发 ----

    def apply_rule(self, entry_id: int, raw_rows: list[dict[str, Any]]) -> dict[str, Any]:
        """按规则表达式清洗原始行，输出去空标准化后的记录。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已启用":
            return {"ok": False, "message": "清洗规则未启用", "cleaned": []}
        cleaned: list[dict[str, Any]] = []
        dropped = 0
        expr = str(entry.get("规则表达式", ""))
        for row in raw_rows:
            values = {k: str(v).strip() for k, v in row.items()}
            if not values.get("实体编码"):
                dropped += 1
                continue
            if "upper" in expr:
                values["实体编码"] = values["实体编码"].upper()
            cleaned.append(values)
        return {"ok": True, "cleaned": cleaned, "dropped": dropped}

    def enqueue_detection(self, entry_id: int) -> dict[str, Any]:
        """把清洗结果排入重复检测队列，写入 dup_detect。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "清洗规则不存在"}
        batches = store.rows("dup_detect")
        batch_id = max((int(r.get("id", 0)) for r in batches), default=0) + 1
        batches.append({
            "id": batch_id,
            "检测批次": f"dup-{entry.get('实体编码')}-{batch_id}",
            "实体编码": entry.get("实体编码", ""),
            "候选组数": "0",
            "相似度算法": "编辑距离",
            "置信区间": "[0.85,1.0]",
            "status": "检测中",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "batch_id": batch_id}

    def handle_anomaly(self, entry_id: int, reason: str) -> str:
        """处理清洗异常：按异常处理策略转人工或忽略。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return "清洗规则不存在"
        strategy = str(entry.get("异常处理", "转人工"))
        if strategy == "转人工":
            reviews = store.rows("merge_review")
            review_id = max((int(r.get("id", 0)) for r in reviews), default=0) + 1
            reviews.append({
                "id": review_id,
                "记录编号": f"rv-{entry_id}-{review_id}",
                "候选组": "",
                "复核结论": "清洗异常",
                "复核角色": "数据管理员",
                "复核时间": datetime.now(timezone.utc).isoformat(),
                "status": "待复核",
                "pending": True,
                "abnormal": True,
            })
            return f"异常已转人工复核: {reason}"
        entry["abnormal"] = False
        return f"异常已按策略忽略: {reason}"

    def consume_extract_output(self, entry_id: int) -> list[str]:
        """读取抽取作业产物，聚合待清洗批次。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return []
        entity_code = entry.get("实体编码", "")
        return [j.get("作业名称", "") for j in store.rows("record_extract")
                if j.get("实体编码") == entity_code and j.get("status") == "已完成"]

    def produce_quality_input(self, entry_id: int, cleaned_count: int) -> int:
        """把清洗结果作为质量检查输入，经统一入口登记到 quality_result。

        清洗产物登记为待检查（生成中、命中数 0）；入口输入输出不变。
        同一清洗规则同一轮重复回传时返回已登记的那份报告编号，
        由本模块（原受理方）补办登记，报告端不接管本批处理资格。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return 0
        receipt = _report_service().register_report_input(
            source="std_cleanse",
            round_key=f"cleanse-{entry_id}",
            entity_code=str(entry.get("实体编码", "")),
            total=cleaned_count,
            hits=0,
            confirmed=False,
            batch_label=str(entry.get("实体编码", "")),
        )
        return int(receipt["report_id"]) if receipt.get("ok") else 0

    def batch_enable(self, entity_code: str) -> int:
        """批量启用某实体下的全部草稿清洗规则。"""
        enabled = 0
        for rule in store.rows(self.MODULE):
            if rule.get("实体编码") == entity_code and rule.get("status") == "草稿":
                rule["status"] = "已启用"
                rule["pending"] = True
                enabled += 1
        return enabled
