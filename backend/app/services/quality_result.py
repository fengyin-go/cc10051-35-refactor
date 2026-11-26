"""质量报告领域服务：管理按规则维度生成的主数据质量报告、评分与失败重试。

数据链路位置：质量治理层报告端，负责消费规则命中记录，
把评分结果推送到治理看板快照。

标准化清洗与质量规则库的报告登记统一收拢到本服务的
``register_report_input``：两个来源共用一份写入与回查口径，
页面按来源标记区分两类报告，历史数据没有该标记、不会被错认。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 报告来源登记：两个入口共用同一份映射，页面与看板按它区分来源。
REPORT_SOURCES = {
    "std_cleanse": "标准化清洗",
    "quality_rule": "质量规则库",
}

# 回执状态：零检查、尚不可用（待受理）、已确认回执互相分开。
RECEIPT_STATES = ("待受理", "已确认", "零检查", "生成失败")


def _to_int(raw: Any) -> int | None:
    """宽松解析整数；历史脏数据返回 None 而不是抛错。"""
    try:
        return int(str(raw or "").strip())
    except (TypeError, ValueError):
        return None


class QualityResultService(BaseModuleService):
    MODULE = "quality_result"
    ENTITY = "质量结果"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["报告批次", "实体编码", "检查总数", "命中数", "评分结果"]
    REQUIRED_FIELDS = ["报告批次", "实体编码", "检查总数"]
    STATUS_ORDER = ["生成中", "已生成", "生成失败"]
    ACTION_RULES = {"生成报告": "已生成", "重试生成": "生成失败"}
    NEGATIVE_ACTIONS = []

    # ---- 统一登记入口：清洗与规则两处登记收拢到这一个写入点 ----

    def register_report_input(
        self,
        *,
        source: str,
        round_key: str,
        entity_code: str,
        total: int,
        hits: int,
        confirmed: bool,
        batch_label: str,
    ) -> dict[str, Any]:
        """登记一份质量报告输入，返回登记回执。

        - 清洗产物登记为待检查（生成中、命中数 0）；规则报告登记时
          已带实际命中（已生成、评分随批写入），两类来源各自标记。
        - 幂等：同一来源且同一执行轮的输入重复回传时，返回已登记的
          那份有效报告，不重复插入、不重新累计。
        - 编号分配与落库在同一写入点内连续完成；登记失败返回
          ok=False，调用方拿不到其他来源刚插入的报告编号。
        """
        if source not in REPORT_SOURCES:
            return {"ok": False, "message": f"未知报告来源：{source}", "report_id": None}
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("source") == source and row.get("执行轮次") == round_key:
                return {
                    "ok": True,
                    "report_id": int(row.get("id", 0)),
                    "created": False,
                    "status": row.get("status"),
                    "source": source,
                    "message": "该来源本轮输入已登记，返回原报告编号",
                }
        report_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        if confirmed:
            score = f"{(1 - hits / total):.2f}" if total else "1.00"
            row = {
                "id": report_id,
                "报告批次": f"qr-{batch_label}-{report_id}",
                "实体编码": entity_code,
                "检查总数": str(total),
                "命中数": str(hits),
                "评分结果": score,
                "status": "已生成",
                "pending": False,
                "abnormal": hits > 0,
            }
        else:
            row = {
                "id": report_id,
                "报告批次": f"qr-{batch_label}-{report_id}",
                "实体编码": entity_code,
                "检查总数": str(total),
                "命中数": "0",
                "评分结果": "pending",
                "status": "生成中",
                "pending": True,
                "abnormal": False,
            }
        row["source"] = source
        row["来源"] = REPORT_SOURCES[source]
        row["执行轮次"] = round_key
        rows.append(row)
        return {
            "ok": True,
            "report_id": report_id,
            "created": True,
            "status": row["status"],
            "source": source,
            "message": "报告输入已登记",
        }

    # ---- 共用回查：报告页与两个来源页用同一套口径 ----

    def receipt_state(self, entry: dict[str, Any]) -> str:
        """单条报告的回执状态：零检查、待受理、已确认、生成失败分开。"""
        status = entry.get("status")
        if status == "已生成":
            total = _to_int(entry.get("检查总数"))
            return "零检查" if total == 0 else "已确认"
        if status == "生成失败":
            return "生成失败"
        return "待受理"

    def intake_receipt(self, source: str, round_key: str) -> dict[str, Any]:
        """按来源与执行轮回查登记回执。

        已确认（原确认已完成）、待受理（收到请求但尚未登记完成）、
        真实无检查（该来源本轮无登记）是三种不同结果；失败时返回
        当前批次的实际来源和状态，便于核对。
        """
        if source not in REPORT_SOURCES:
            return {"ok": False, "state": "none",
                    "message": f"未知报告来源：{source}"}
        for row in store.rows(self.MODULE):
            if row.get("source") == source and row.get("执行轮次") == round_key:
                state = {
                    "已生成": "confirmed",
                    "生成中": "pending",
                    "生成失败": "failed",
                }.get(str(row.get("status")), "pending")
                return {
                    "ok": True,
                    "state": state,
                    "report_id": row.get("id"),
                    "报告批次": row.get("报告批次", ""),
                    "来源": row.get("来源", REPORT_SOURCES[source]),
                    "status": row.get("status", ""),
                    "回执状态": self.receipt_state(row),
                    "检查总数": row.get("检查总数", "0"),
                    "命中数": row.get("命中数", "0"),
                }
        return {"ok": True, "state": "none",
                "message": "该来源本轮尚无登记，属真实无检查"}

    def receipt_summary(self) -> dict[str, int]:
        """把零检查、尚不可用、已确认回执分开计数，供报告页与看板消费。"""
        summary = {state: 0 for state in RECEIPT_STATES}
        for row in store.rows(self.MODULE):
            summary[self.receipt_state(row)] += 1
        return summary

    # ---- 业务扩展：质量报告生成与看板推送 ----

    def generate_report(self, entry_id: int) -> dict[str, Any]:
        """汇总规则命中生成质量报告，计算评分。

        只引用本条目已登记的那一份输入（对应实体与执行轮的
        检查数及命中数），不读取其他来源的报告数据。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "生成中":
            return {"ok": False, "message": "报告不在生成中状态"}
        total = int(entry.get("检查总数", "0") or 0)
        hits = int(entry.get("命中数", "0") or 0)
        if hits > total:
            entry["status"] = "生成失败"
            entry["abnormal"] = True
            return {"ok": False, "message": "命中数超过检查总数，数据异常"}
        score = 1 - (hits / total if total else 0)
        entry["评分结果"] = f"{score:.2%}"
        entry["status"] = "已生成"
        entry["pending"] = False
        return {"ok": True, "score": f"{score:.2%}", "total": total, "hits": hits}

    def retry_generate(self, entry_id: int, fix_hint: str) -> dict[str, Any]:
        """失败重试：校验数据修复后重新生成。

        只重置本条目状态，来源与执行轮次保持登记时的原值，
        不带入另一入口的来源信息。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "质量结果不存在"}
        if entry.get("status") != "生成失败":
            return {"ok": False, "message": "报告未失败，无需重试"}
        entry["status"] = "生成中"
        entry["pending"] = True
        entry["修复说明"] = fix_hint
        return {"ok": True, "message": "已重入生成队列"}

    def consume_rule_hits(self, entry_id: int) -> list[dict[str, Any]]:
        """读取质量规则库的命中记录，作为报告数据源。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return []
        return [r for r in store.rows("quality_rule")
                if r.get("实体编码") == entry.get("实体编码")
                and int(r.get("今日命中", "0") or 0) > 0]

    def push_stats_overview(self, entry_id: int) -> int:
        """把评分结果推送到治理看板快照。

        快照附带报告来源与回执状态（新增字段，旧格式照常消费），
        看板侧据此把零检查、尚不可用、已确认回执分开。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已生成":
            return 0
        stats = store.rows("stats_overview")
        stat_id = max((int(r.get("id", 0)) for r in stats), default=0) + 1
        score = str(entry.get("评分结果", "0%"))
        abnormal = "指标越界" if score.rstrip("%") < "90" else "正常"
        stats.append({
            "id": stat_id,
            "统计时间": datetime.now(timezone.utc).isoformat(),
            "实体编码": entry.get("实体编码", ""),
            "金记录数": entry.get("检查总数", "0"),
            "重复率": score,
            "分发延迟": "0",
            "status": abnormal,
            "pending": abnormal != "正常",
            "abnormal": abnormal != "正常",
            "来源": entry.get("来源", "历史数据"),
            "回执状态": self.receipt_state(entry),
        })
        return stat_id

    def export_report_markdown(self, entry_id: int) -> str:
        """导出质量报告为 Markdown 文本。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return ""
        lines = [
            f"# 质量报告 {entry.get('报告批次')}",
            f"- 实体编码: {entry.get('实体编码')}",
            f"- 检查总数: {entry.get('检查总数')}",
            f"- 命中数: {entry.get('命中数')}",
            f"- 评分结果: {entry.get('评分结果')}",
        ]
        return "\n".join(lines)

    def filter_by_score(self, min_score: float) -> list[dict[str, Any]]:
        """按评分阈值筛选低质量报告，供回捞重算。"""
        result: list[dict[str, Any]] = []
        for r in store.rows(self.MODULE):
            if r.get("status") != "已生成":
                continue
            raw = str(r.get("评分结果", "0%")).rstrip("%")
            try:
                if float(raw) < min_score * 100:
                    result.append(r)
            except ValueError:
                continue
        return result
