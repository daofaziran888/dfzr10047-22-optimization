"""解析器路由：首选解析器失败→按备选顺序回退→回退计数供指标结算。

核心行为：
- 按探测认定的真实格式选择首选解析器，声明格式与真实格式不一致时以真实格式为准
- 首选解析器失败按备选清单顺序回退，回退次数逐次累加
- 备选清单耗尽仍失败时判定路由失败，该批次不进入字段解析
- 每次尝试携带各自的输入凭据并持久化到运行记录，迟到确认与恢复重试不重复计数
"""
from __future__ import annotations

from typing import Any


class RouteAttempt:
    """单个解析器的一次实际尝试：输入凭据、结果与确认状态。"""

    def __init__(self, receipt_no: str, parser: str, input_credential: str,
                 success: bool, output_fields: str, row_count: int,
                 attempt_time: str, failure_reason: str = ""):
        self.receipt_no = receipt_no
        self.parser = parser
        self.input_credential = input_credential
        self.success = success
        self.output_fields = output_fields
        self.row_count = row_count
        self.attempt_time = attempt_time
        self.failure_reason = failure_reason
        self.confirm_state = "待核对" if success else "已失败"
        self.late = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_no": self.receipt_no,
            "parser": self.parser,
            "input_credential": self.input_credential,
            "success": self.success,
            "output_fields": self.output_fields,
            "row_count": self.row_count,
            "confirm_state": self.confirm_state,
            "late": self.late,
            "attempt_time": self.attempt_time,
            "failure_reason": self.failure_reason,
        }


class RouteDecision:
    """单次路由决策。"""

    def __init__(self, decision_id: str, batch_id: str, resolved_format: str,
                 primary_parser: str, fallback_parsers: list[str], basis: str):
        self.decision_id = decision_id
        self.batch_id = batch_id
        self.resolved_format = resolved_format
        self.primary_parser = primary_parser
        self.fallback_parsers = list(fallback_parsers)
        self.basis = basis
        self.selected_parser = ""
        self.fallback_count = 0
        self.status = "待路由"
        self.decide_time = ""
        # 实际执行过的尝试清单（含失败与迟到成功），恢复与台账以此为准
        self.attempts: list[RouteAttempt] = []
        self._receipt_seq = 0

    def record_attempt(self, parser: str, input_credential: str, success: bool,
                       output_fields: str = "", row_count: int = 0,
                       attempt_time: str = "",
                       failure_reason: str = "") -> RouteAttempt:
        """登记一次实际尝试并返回运行回执；同解析器同凭据幂等返回原回执。"""
        for attempt in self.attempts:
            if attempt.parser == parser and attempt.input_credential == input_credential:
                return attempt
        self._receipt_seq += 1
        receipt_no = f"FR-{self.decision_id}-{self._receipt_seq:02d}"
        attempt = RouteAttempt(
            receipt_no, parser, input_credential, success,
            output_fields, row_count, attempt_time, failure_reason)
        self.attempts.append(attempt)
        return attempt

    def confirm_attempt(self, receipt_no: str, late: bool = False) -> RouteAttempt | None:
        """核对迟到的成功回执：只改确认状态，不重新执行解析器。"""
        for attempt in self.attempts:
            if attempt.receipt_no == receipt_no and attempt.success:
                if attempt.confirm_state == "待核对":
                    attempt.confirm_state = "已确认"
                    attempt.late = late
                return attempt
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "batch_id": self.batch_id,
            "resolved_format": self.resolved_format,
            "primary_parser": self.primary_parser,
            "fallback_parsers": self.fallback_parsers,
            "selected_parser": self.selected_parser,
            "fallback_count": self.fallback_count,
            "status": self.status,
            "basis": self.basis,
            "decide_time": self.decide_time,
            "attempts": [a.to_dict() for a in self.attempts],
        }


class ParserRouteEngine:
    """解析器路由引擎。"""

    def __init__(self) -> None:
        self._format_parsers: dict[str, list[str]] = {}
        self._decisions: dict[str, RouteDecision] = {}

    def register_parsers(self, fmt: str, parsers: list[str]) -> None:
        """注册格式的解析器优先级清单：首个为首选，其余为备选。"""
        self._format_parsers[fmt] = list(parsers)

    def route(self, decision_id: str, batch_id: str, resolved_format: str,
              basis: str, decide_time: str) -> tuple[bool, RouteDecision | None]:
        """按认定格式选定首选解析器。"""
        parsers = self._format_parsers.get(resolved_format, [])
        if not parsers:
            decision = RouteDecision(decision_id, batch_id, resolved_format,
                                     "", [], basis)
            decision.status = "路由失败"
            decision.decide_time = decide_time
            self._decisions[decision_id] = decision
            return False, decision

        decision = RouteDecision(decision_id, batch_id, resolved_format,
                                 parsers[0], parsers[1:], basis)
        decision.selected_parser = parsers[0]
        decision.status = "已选定"
        decision.decide_time = decide_time
        self._decisions[decision_id] = decision
        return True, decision

    def execute_with_fallback(self, decision_id: str,
                              try_parse: Any,
                              fail_time: str) -> tuple[bool, str, RouteDecision | None]:
        """执行首选解析器，失败时按备选顺序回退。

        try_parse(parser_id) -> bool 由调用方提供真实解析成败判定。
        """
        decision = self._decisions.get(decision_id)
        if decision is None:
            return False, f"路由决策 {decision_id} 不存在", None

        if decision.status == "路由失败":
            return False, (
                f"批次 {decision.batch_id} 格式 {decision.resolved_format} "
                f"无可用解析器，路由失败"
            ), decision

        candidates = [decision.primary_parser] + decision.fallback_parsers
        for index, parser in enumerate(candidates):
            # 每次尝试使用各自的输入凭据，失败重试与迟到确认据此区分
            credential = f"digest:{decision.batch_id}:{parser}#attempt-{index}"
            success = bool(try_parse(parser))
            attempt = decision.record_attempt(
                parser, credential, success,
                output_fields=f"fields-of-{parser}" if success else "",
                row_count=0, attempt_time=fail_time,
                failure_reason="" if success else f"{parser} 解析失败")
            if success:
                decision.confirm_attempt(attempt.receipt_no)
                decision.selected_parser = parser
                decision.fallback_count = index
                decision.status = "已回退" if index > 0 else "已选定"
                decision.decide_time = fail_time
                if index > 0:
                    return True, (
                        f"首选解析器 {decision.primary_parser} 失败，"
                        f"回退到 {parser} 成功（回退 {index} 次，回执 {attempt.receipt_no}）"
                    ), decision
                return True, f"首选解析器 {parser} 命中，无需回退", decision

        decision.fallback_count = len(decision.fallback_parsers) + 1
        decision.status = "路由失败"
        decision.decide_time = fail_time
        return False, (
            f"批次 {decision.batch_id} 全部 {len(candidates)} 个解析器均失败，判定路由失败"
        ), decision

    def reconcile(self, decision_id: str) -> dict[str, Any] | None:
        """恢复核对：分开已产出待核对、失败尝试与尚未执行的备选。"""
        decision = self._decisions.get(decision_id)
        if decision is None:
            return None
        candidates = [decision.primary_parser] + decision.fallback_parsers
        executed = {a.parser for a in decision.attempts}
        return {
            "decision_id": decision_id,
            "chain": candidates,
            "selected_parser": decision.selected_parser,
            "status": decision.status,
            "fallback_count": decision.fallback_count,
            "pending_review": [
                a.to_dict() for a in decision.attempts
                if a.confirm_state in ("待核对", "已确认")
            ],
            "failed_attempts": [
                a.to_dict() for a in decision.attempts if not a.success
            ],
            "not_started": [p for p in candidates if p not in executed],
            "attempts": [a.to_dict() for a in decision.attempts],
        }

    def fallback_stats(self) -> tuple[int, int]:
        """返回 (发生回退的决策数, 决策总数)，供回退率结算。"""
        decisions = list(self._decisions.values())
        fallbacks = sum(1 for d in decisions if d.fallback_count > 0)
        return fallbacks, len(decisions)

    def get_decision(self, decision_id: str) -> RouteDecision | None:
        return self._decisions.get(decision_id)

    def decisions_of_batch(self, batch_id: str) -> list[RouteDecision]:
        return [d for d in self._decisions.values() if d.batch_id == batch_id]

    def snapshot(self) -> dict[str, Any]:
        fallbacks, total = self.fallback_stats()
        return {
            "format_parsers": dict(self._format_parsers),
            "decisions": {k: v.to_dict() for k, v in self._decisions.items()},
            "fallback_count": fallbacks,
            "decision_total": total,
        }
