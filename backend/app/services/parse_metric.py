"""解析指标领域服务：按窗口和来源汇总吞吐、隔离率与回退率统计，产出指标读数数据集。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ParseMetricService(BaseModuleService):
    MODULE = "parse_metric"
    ENTITY = "指标读数"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["指标读数编号", "统计窗口", "来源渠道", "解析吞吐行数", "平均解析耗时", "隔离率", "回退率", "采集时间"]
    REQUIRED_FIELDS = ["指标读数编号", "统计窗口", "来源渠道"]
    STATUS_ORDER = ["统计中", "已结算", "待复核", "已作废"]
    ACTION_RULES = {"提交结算": "已结算", "复核读数": "待复核", "作废读数": "已作废"}
    NEGATIVE_ACTIONS = ["作废读数"]

    @staticmethod
    def _int(row: dict[str, Any], field: str) -> int:
        text = str(row.get(field) or "").strip()
        return int(text) if text.isdigit() else 0

    @staticmethod
    def _float(row: dict[str, Any], field: str) -> float:
        try:
            return float(str(row.get(field) or "").strip())
        except ValueError:
            return 0.0

    def real_quarantine_rate(self, channel: str) -> tuple[float, int, int]:
        """隔离率来自坏行隔离的真实计数，不做估算。

        返回 (隔离率, 隔离行数, 总行数)。总行数取该来源已闭合批次的行数之和。
        """
        intake_batches = {
            str(r.get("批次编号")) for r in store.rows("file_intake")
            if r.get("来源渠道") == channel
        }
        total_rows = 0
        for batch in store.rows("parse_batch"):
            if str(batch.get("关联投递批次")) in intake_batches:
                total_rows += self._int(batch, "总行数")
        quarantined = sum(
            1 for r in store.rows("row_quarantine")
            if str(r.get("关联批次")) in intake_batches
        )
        rate = quarantined / total_rows if total_rows > 0 else 0.0
        return round(rate, 4), quarantined, total_rows

    def real_fallback_rate(self, channel: str) -> tuple[float, int, int]:
        """回退率来自持久化回退运行记录的真实结算，不做估算。

        返回 (回退率, 发生回退的决策数, 决策总数)。有运行台账的决策按台账中
        已采用回执的实际解析器序号结算；无成功凭据的尝试不能把批次写成成功。
        """
        intake_batches = {
            str(r.get("批次编号")) for r in store.rows("file_intake")
            if r.get("来源渠道") == channel
        }
        decisions = [
            r for r in store.rows("parser_route")
            if str(r.get("关联批次")) in intake_batches
        ]
        from app.services import SERVICES as _SERVICES
        route_svc = _SERVICES.get("parser_route")
        fallbacks = 0
        for row in decisions:
            decision_no = str(row.get("决策编号"))
            if route_svc is not None and route_svc._runs(decision_no):
                count = route_svc.ledger_fallback_count(decision_no)
            else:
                count = self._int(row, "回退次数")
            if count > 0:
                fallbacks += 1
        rate = fallbacks / len(decisions) if decisions else 0.0
        return round(rate, 4), fallbacks, len(decisions)

    def trace_runs(self, reading_no: str) -> list[dict[str, Any]]:
        """指标复核页从采用结果回到对应的实际执行记录（回退运行台账）。"""
        row = next(
            (r for r in store.rows(self.MODULE)
             if r.get("指标读数编号") == reading_no), None)
        if row is None:
            return []
        channel = str(row.get("来源渠道") or "")
        intake_batches = {
            str(r.get("批次编号")) for r in store.rows("file_intake")
            if r.get("来源渠道") == channel
        }
        decision_nos = {
            str(r.get("决策编号")) for r in store.rows("parser_route")
            if str(r.get("关联批次")) in intake_batches
        }
        return [
            dict(run) for run in store.rows("fallback_run")
            if str(run.get("决策编号")) in decision_nos
        ]

    def settle_reading(self, reading_no: str, window: str, channel: str,
                       collected_at: str) -> tuple[bool, str, dict[str, Any] | None]:
        """结算指标读数：吞吐、平均耗时、隔离率、回退率全部取自真实明细。"""
        intake_batches = {
            str(r.get("批次编号")) for r in store.rows("file_intake")
            if r.get("来源渠道") == channel
        }
        batches = [
            b for b in store.rows("parse_batch")
            if str(b.get("关联投递批次")) in intake_batches
        ]
        throughput = sum(self._int(b, "总行数") for b in batches)

        durations = [
            self._float(p, "解析耗时") for p in store.rows("field_parse")
            if str(p.get("关联批次")) in intake_batches
            and str(p.get("解析耗时") or "").replace(".", "").isdigit()
        ]
        avg_ms = sum(durations) / len(durations) if durations else 0.0

        quarantine_rate, quarantined, total_rows = self.real_quarantine_rate(channel)
        fallback_rate, fallbacks, decisions = self.real_fallback_rate(channel)

        # 无成功凭据的尝试不能把批次写成成功：台账里已采用回执覆盖的决策才计成功
        from app.services import SERVICES as _SERVICES
        route_svc = _SERVICES.get("parser_route")
        channel_decision_nos = [
            str(r.get("决策编号")) for r in store.rows("parser_route")
            if str(r.get("关联批次")) in intake_batches
        ]
        adopted_count = 0
        run_count = 0
        if route_svc is not None:
            adopted_set = route_svc.successful_decisions()
            adopted_count = sum(1 for no in channel_decision_nos if no in adopted_set)
            run_count = sum(
                1 for run in store.rows("fallback_run")
                if str(run.get("决策编号")) in set(channel_decision_nos)
            )

        rows = store.rows(self.MODULE)
        target = next((r for r in rows if r.get("指标读数编号") == reading_no), None)
        if target is None:
            entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            target = {"id": entry_id, "指标读数编号": reading_no}
            rows.append(target)
        target.update({
            "统计窗口": window,
            "来源渠道": channel,
            "解析吞吐行数": str(throughput),
            "平均解析耗时": f"{avg_ms:.2f}",
            "隔离率": f"{quarantine_rate:.4f}",
            "回退率": f"{fallback_rate:.4f}",
            "采集时间": collected_at,
            "隔离行计数": str(quarantined),
            "批次总行计数": str(total_rows),
            "回退决策计数": str(fallbacks),
            "路由决策计数": str(decisions),
            "回退运行记录数": str(run_count),
            "成功凭据决策数": str(adopted_count),
            "status": "已结算",
            "pending": False,
            "abnormal": False,
        })
        return True, (
            f"指标读数 {reading_no} 已结算：吞吐 {throughput} 行，"
            f"隔离率 {quarantine_rate:.2%}（{quarantined}/{total_rows}），"
            f"回退率 {fallback_rate:.2%}（{fallbacks}/{decisions}）"
        ), dict(target)

    def needs_review(self, reading_no: str, reason: str) -> tuple[bool, str]:
        """标记待复核：结算读数与其他模块真实计数对不上时使用。"""
        for row in store.rows(self.MODULE):
            if row.get("指标读数编号") == reading_no:
                row["status"] = "待复核"
                row["pending"] = True
                row["abnormal"] = True
                row["复核原因"] = reason
                return True, f"指标读数 {reading_no} 已转入待复核：{reason}"
        return False, f"指标读数 {reading_no} 不存在"

    def cross_check(self, reading_no: str) -> tuple[bool, str]:
        """与坏行隔离、解析路由的真实计数交叉核对，防止估算值入库。"""
        row = next(
            (r for r in store.rows(self.MODULE)
             if r.get("指标读数编号") == reading_no), None)
        if row is None:
            return False, f"指标读数 {reading_no} 不存在"

        channel = str(row.get("来源渠道") or "")
        rate, quarantined, total_rows = self.real_quarantine_rate(channel)
        recorded_quarantined = self._int(row, "隔离行计数")
        if recorded_quarantined != quarantined:
            return False, (
                f"读数 {reading_no} 记录的隔离行 {recorded_quarantined} "
                f"与坏行隔离真实计数 {quarantined} 不一致，读数不得结算"
            )
        recorded_rate = self._float(row, "隔离率")
        if abs(recorded_rate - rate) > 0.0001:
            return False, (
                f"读数 {reading_no} 隔离率 {recorded_rate:.4f} "
                f"与真实计数 {rate:.4f}（{quarantined}/{total_rows}）不一致"
            )

        # 回退率必须与持久化回退运行记录的结算读数一致
        fb_rate, fb_decisions, total_decisions = self.real_fallback_rate(channel)
        recorded_fb = self._int(row, "回退决策计数")
        if recorded_fb != fb_decisions:
            return False, (
                f"读数 {reading_no} 记录的回退决策 {recorded_fb} "
                f"与回退运行台账结算 {fb_decisions} 不一致，读数不得结算"
            )
        recorded_fb_rate = self._float(row, "回退率")
        if abs(recorded_fb_rate - fb_rate) > 0.0001:
            return False, (
                f"读数 {reading_no} 回退率 {recorded_fb_rate:.4f} "
                f"与台账真实结算 {fb_rate:.4f}（{fb_decisions}/{total_decisions}）不一致"
            )
        return True, (
            f"读数 {reading_no} 与真实计数一致：隔离 {quarantined}/{total_rows} = {rate:.4f}，"
            f"回退 {fb_decisions}/{total_decisions} = {fb_rate:.4f}（可追溯 {len(self.trace_runs(reading_no))} 条运行回执）"
        )

    def channel_throughput(self) -> dict[str, int]:
        """按来源渠道汇总吞吐行数，供分析看板消费。"""
        totals: dict[str, int] = {}
        for row in store.rows(self.MODULE):
            if row.get("status") == "已作废":
                continue
            channel = str(row.get("来源渠道") or "")
            totals[channel] = totals.get(channel, 0) + self._int(row, "解析吞吐行数")
        return totals
