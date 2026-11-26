"""解析路由领域服务：按探测出的目标格式选择解析器并管理回退策略，产出路由决策数据集。

回退可靠性设计（运行台账 fallback_run 持久化）：
- 每次尝试都落一条运行记录：实际解析器 + 输入凭据（摘要）+ 解析产出 + 确认状态
- 同一 (决策, 解析器, 输入凭据) 重复提交幂等返回原回执，不重复执行、不累加次数
- 尝试成功先进入「待核对」，迟到确认只核对原回执；值班员采用后才成为唯一有效产物
- 恢复时先核对已执行解析器与迟到结果，再区分「已产出待核对」与「尚未执行」的备选
- 回退次数与解析指标都以持久化台账为唯一结算依据，无成功凭据的决策不得写成成功。
"""
from __future__ import annotations

from typing import Any, Callable

from app.services.base import BaseModuleService
from app.store import store

RUN_MODULE = "fallback_run"
# 决策创建时固化的解析器链快照：采用动作会改写「命中解析器」用于展示，
# 但回退序号与旧尝试必须以决策当时的链为准，配置变更也不追溯改写。
CHAIN_SNAPSHOT_FIELD = "解析器链快照"

# 运行记录确认状态
ST_PENDING = "待核对"      # 解析入口已回传成功产出，等待服务端核对/采用
ST_CONFIRMED = "已确认"    # 服务端已核对解析入口返回的确认
ST_FAILED = "已失败"       # 解析入口返回失败，保留原输入供展开
ST_ADOPTED = "已采用"      # 值班员凭回执采用，成为该决策唯一有效产物
ST_SUPERSEDED = "已搁置"   # 已有采用产物后迟到的其他成功确认，不再有效

CONFIRM_STATES = {ST_PENDING, ST_CONFIRMED, ST_FAILED, ST_ADOPTED, ST_SUPERSEDED}
# 可以被值班员采用为合法恢复尝试的状态
ADOPTABLE_STATES = {ST_PENDING, ST_CONFIRMED}
# 已经真实执行过（解析入口给过结论）的状态
EXECUTED_STATES = {ST_PENDING, ST_CONFIRMED, ST_FAILED, ST_ADOPTED, ST_SUPERSEDED}


class ParserRouteService(BaseModuleService):
    MODULE = "parser_route"
    ENTITY = "路由决策"
    OPERATION_TERM = "调度"
    LIST_FIELDS = ["决策编号", "关联批次", "推断格式", "命中解析器", "备选解析器", "路由依据", "回退次数", "决策时间"]
    REQUIRED_FIELDS = ["决策编号", "关联批次", "推断格式"]
    STATUS_ORDER = ["待路由", "已选定", "已回退", "路由失败"]
    ACTION_RULES = {"选定解析器": "已选定", "执行回退": "已回退", "判定失败": "路由失败"}
    NEGATIVE_ACTIONS = ["判定失败"]

    # 每种格式允许的解析器优先级清单，首个为首选，其余按顺序回退
    PARSER_CHAINS: dict[str, list[str]] = {
        "csv-utf8": ["csv_parser_v2", "csv_parser_v1", "generic_delimiter"],
        "csv-utf16le": ["csv_parser_v2", "generic_delimiter"],
        "tsv": ["tsv_parser_v1", "generic_delimiter"],
        "xlsx": ["xlsx_parser_v3", "xlsx_parser_v2", "generic_xml_parser"],
        "jsonl": ["jsonl_parser_v1", "generic_jsonl"],
        "jsonl-crlf": ["jsonl_parser_v1", "generic_jsonl"],
        "xml": ["xml_parser_v1"],
    }

    # ------------------------------------------------------------------
    # 基础查询
    # ------------------------------------------------------------------
    def _find(self, decision_no: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if row.get("决策编号") == decision_no:
                return row
        return None

    def _runs(self, decision_no: str) -> list[dict[str, Any]]:
        return [
            r for r in store.rows(RUN_MODULE)
            if r.get("决策编号") == decision_no
        ]

    def _find_run(self, receipt_no: str) -> dict[str, Any] | None:
        return next(
            (r for r in store.rows(RUN_MODULE) if r.get("运行回执编号") == receipt_no),
            None,
        )

    def _chain_of(self, decision: dict[str, Any]) -> list[str]:
        """决策当时配置的解析器链（首选 + 备选），优先读取持久化链快照。"""
        snapshot = str(decision.get(CHAIN_SNAPSHOT_FIELD) or "").strip()
        if snapshot:
            chain = [p.strip() for p in snapshot.split(",") if p.strip()]
        else:
            primary = str(decision.get("命中解析器") or "").strip()
            fallbacks = [
                p.strip() for p in str(decision.get("备选解析器") or "").split(",") if p.strip()
            ]
            chain = ([primary] if primary else []) + fallbacks
            # 首次读取即固化快照：采用动作之后「命中解析器」会被改写，
            # 没有快照会丢失首选，配置变更也不能追溯旧尝试。
            if chain:
                decision[CHAIN_SNAPSHOT_FIELD] = ",".join(chain)
        # 去重保持顺序
        seen: set[str] = set()
        ordered: list[str] = []
        for parser in chain:
            if parser not in seen:
                seen.add(parser)
                ordered.append(parser)
        return ordered

    def _next_receipt(self) -> str:
        rows = store.rows(RUN_MODULE)
        return f"FR-{max((int(r.get('id', 0)) for r in rows), default=0) + 1:04d}"

    def _next_run_index(self, decision_no: str) -> int:
        indexes = [
            int(r.get("回退序号") or 0) for r in self._runs(decision_no)
            if str(r.get("回退序号") or "").isdigit()
        ]
        return (max(indexes) + 1) if indexes else 0

    def _adopted_run(self, decision_no: str) -> dict[str, Any] | None:
        return next(
            (r for r in self._runs(decision_no) if r.get("确认状态") == ST_ADOPTED),
            None,
        )

    # ------------------------------------------------------------------
    # 路由选定
    # ------------------------------------------------------------------
    def choose_by_probe(self, decision_no: str, intake_batch: str,
                        probe_no: str, decide_time: str) -> tuple[bool, str, dict[str, Any] | None]:
        """按格式探测的真实格式选定首选解析器。

        探测结论为存疑或无法识别时不得选定解析器，批次转为路由失败。
        """
        probe = next(
            (r for r in store.rows("format_probe") if r.get("探测编号") == probe_no), None)
        if probe is None:
            return False, f"探测结论 {probe_no} 不存在，无法路由", None

        resolved_format = str(probe.get("推断格式") or "")
        confidence = str(probe.get("置信度") or "0")
        try:
            confidence_value = float(confidence)
        except ValueError:
            confidence_value = 0.0

        rows = store.rows(self.MODULE)
        entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        chain = self.PARSER_CHAINS.get(resolved_format, [])

        if probe.get("status") in ("存疑", "无法识别") or confidence_value < 0.5:
            entry = {
                "id": entry_id, "status": "路由失败", "pending": False, "abnormal": True,
                "决策编号": decision_no, "关联批次": intake_batch,
                "推断格式": resolved_format, "命中解析器": "",
                "备选解析器": ",".join(chain),
                "路由依据": (
                    f"探测状态 {probe.get('status')}、置信度 {confidence}，"
                    f"不足以选定解析器"
                ),
                "回退次数": str(len(chain)), "决策时间": decide_time,
            }
            rows.append(entry)
            return False, (
                f"批次 {intake_batch} 探测置信度 {confidence} 不足，路由失败"
            ), dict(entry)

        if not chain:
            entry = {
                "id": entry_id, "status": "路由失败", "pending": False, "abnormal": True,
                "决策编号": decision_no, "关联批次": intake_batch,
                "推断格式": resolved_format, "命中解析器": "", "备选解析器": "",
                "路由依据": f"格式 {resolved_format} 无注册解析器链",
                "回退次数": "0", "决策时间": decide_time,
            }
            rows.append(entry)
            return False, f"格式 {resolved_format} 没有注册解析器，路由失败", dict(entry)

        entry = {
            "id": entry_id, "status": "已选定", "pending": False, "abnormal": False,
            "决策编号": decision_no, "关联批次": intake_batch,
            "推断格式": resolved_format, "命中解析器": chain[0],
            "备选解析器": ",".join(chain[1:]),
            CHAIN_SNAPSHOT_FIELD: ",".join(chain),
            "路由依据": f"探测格式 {resolved_format}（置信度 {confidence}）命中解析器链首项",
            "回退次数": "0", "决策时间": decide_time,
        }
        rows.append(entry)
        return True, (
            f"批次 {intake_batch} 选定解析器 {chain[0]}，备选 {chain[1:]}"
        ), dict(entry)

    # ------------------------------------------------------------------
    # 回退运行台账：提交尝试 / 迟到确认 / 采用回执 / 恢复核对
    # ------------------------------------------------------------------
    def submit_attempt(
        self, decision_no: str, parser: str, input_credential: str,
        success: bool, output_fields: str, row_count: int, attempt_time: str,
        failure_reason: str = "",
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """值班员/解析入口提交一次解析尝试，服务端落持久化运行回执。

        幂等键为 (决策编号, 实际解析器, 输入凭据)：同凭据重复提交直接返回原回执，
        不重复执行也不累加回退次数。成功产出先进入「待核对」，失败保留原输入。
        """
        decision = self._find(decision_no)
        if decision is None:
            return False, f"路由决策 {decision_no} 不存在", None
        if decision.get("status") == "路由失败" and not self._runs(decision_no):
            return False, f"决策 {decision_no} 已判定路由失败，无解析器可执行", None

        parser = (parser or "").strip()
        input_credential = (input_credential or "").strip()
        if not parser:
            return False, "实际解析器不能为空", None
        if not input_credential:
            return False, "输入凭据不能为空，恢复与重试必须携带各自的输入凭据", None

        chain = self._chain_of(decision)
        if chain and parser not in chain:
            return False, (
                f"解析器 {parser} 不在决策 {decision_no} 当前配置的解析器链 "
                f"{chain} 内，配置变更前的旧尝试仍保留在台账中但不得新增执行"
            ), None

        adopted = self._adopted_run(decision_no)
        if adopted is not None and adopted.get("实际解析器") != parser:
            return False, (
                f"决策 {decision_no} 已采用回执 {adopted.get('运行回执编号')} "
                f"（{adopted.get('实际解析器')}），不得再执行其他解析器"
            ), None

        # 幂等：同解析器 + 同输入凭据只认第一条运行记录
        duplicate = next(
            (r for r in self._runs(decision_no)
             if r.get("实际解析器") == parser and r.get("输入凭据") == input_credential),
            None,
        )
        if duplicate is not None:
            return True, (
                f"解析器 {parser} 携带相同输入凭据已登记为回执 "
                f"{duplicate.get('运行回执编号')}（{duplicate.get('确认状态')}），"
                f"重复提交不再执行也不累加回退次数"
            ), dict(duplicate)

        run_index = self._next_run_index(decision_no)
        receipt_no = self._next_receipt()
        if success:
            confirm_state = ST_PENDING
            row_status, abnormal = ST_PENDING, False
        else:
            confirm_state = ST_FAILED
            row_status, abnormal = ST_FAILED, True
        run = {
            "id": max((int(r.get("id", 0)) for r in store.rows(RUN_MODULE)), default=0) + 1,
            "status": row_status,
            "pending": confirm_state in (ST_PENDING,),
            "abnormal": abnormal,
            "运行回执编号": receipt_no,
            "决策编号": decision_no,
            "关联批次": decision.get("关联批次"),
            "实际解析器": parser,
            "回退序号": str(run_index),
            "输入凭据": input_credential,
            "解析行数": str(max(0, int(row_count))),
            "确认状态": confirm_state,
            "产出字段": output_fields or "",
            "迟到标记": "否",
            "尝试时间": attempt_time,
            "确认时间": "",
            "失败原因": failure_reason or "",
        }
        store.rows(RUN_MODULE).append(run)
        if success:
            message = (
                f"回执 {receipt_no} 已登记：{parser} 产出 {row_count} 行，"
                f"状态待核对，需服务端确认后才能采用"
            )
        else:
            message = (
                f"回执 {receipt_no} 已登记：{parser} 解析失败，原输入凭据已留存"
            )
        return True, message, dict(run)

    def confirm_run(self, receipt_no: str, confirm_time: str,
                    late: bool = False) -> tuple[bool, str, dict[str, Any] | None]:
        """服务端核对解析入口迟到的成功回执：只核对已登记记录，不重新执行。

        迟到确认若到达时决策已采用其他产物，该回执标记为「已搁置」，不覆盖唯一产物。
        """
        run = self._find_run(receipt_no)
        if run is None:
            return False, f"运行回执 {receipt_no} 不存在", None
        if run.get("确认状态") == ST_FAILED:
            return False, (
                f"回执 {receipt_no} 是失败尝试（{run.get('实际解析器')}），"
                f"不能确认成功，失败重试须使用各自的输入凭据重新提交"
            ), None
        if run.get("确认状态") in (ST_CONFIRMED, ST_ADOPTED):
            return True, (
                f"回执 {receipt_no} 已处于{run.get('确认状态')}，"
                f"重复确认不产生新的执行记录"
            ), dict(run)

        adopted = self._adopted_run(str(run.get("决策编号")))
        if adopted is not None and adopted.get("运行回执编号") != receipt_no:
            run["确认状态"] = ST_SUPERSEDED
            run["status"] = ST_SUPERSEDED
            run["pending"] = False
            run["迟到标记"] = "是"
            run["确认时间"] = confirm_time
            return True, (
                f"回执 {receipt_no} 迟到：决策已采用 {adopted.get('运行回执编号')}，"
                f"该回执搁置且不覆盖已采用字段"
            ), dict(run)

        run["确认状态"] = ST_CONFIRMED
        run["status"] = ST_CONFIRMED
        run["pending"] = False
        run["迟到标记"] = "是" if late else run.get("迟到标记", "否")
        run["确认时间"] = confirm_time
        return True, (
            f"回执 {receipt_no}（{run.get('实际解析器')}）已与解析入口确认一致，"
            f"可凭回执采用"
        ), dict(run)

    def adopt_run(self, decision_no: str, receipt_no: str,
                  adopt_time: str) -> tuple[bool, str, dict[str, Any] | None]:
        """值班员凭回执采用一份解析产物，同步路由决策与运行台账。

        同一决策只允许一份「已采用」产物：并发提交 P2/P3 时只确认一份实际字段，
        已采用尝试只计一次，其他迟到成功回执自动搁置。
        """
        decision = self._find(decision_no)
        if decision is None:
            return False, f"路由决策 {decision_no} 不存在", None
        run = self._find_run(receipt_no)
        if run is None or run.get("决策编号") != decision_no:
            return False, f"回执 {receipt_no} 不属于决策 {decision_no}", None
        if run.get("确认状态") == ST_ADOPTED:
            return True, (
                f"回执 {receipt_no} 已是决策 {decision_no} 的唯一有效产物，"
                f"重复采用不累加回退次数"
            ), dict(run)
        if run.get("确认状态") not in ADOPTABLE_STATES:
            return False, (
                f"回执 {receipt_no} 当前为{run.get('确认状态')}，"
                f"只有待核对/已确认回执可以采用"
            ), None

        adopted = self._adopted_run(decision_no)
        if adopted is not None:
            return False, (
                f"决策 {decision_no} 已采用回执 {adopted.get('运行回执编号')}，"
                f"采用尝试只算一次，不能再采用 {receipt_no}"
            ), None

        run["确认状态"] = ST_ADOPTED
        run["status"] = ST_ADOPTED
        run["pending"] = False
        run["迟到标记"] = run.get("迟到标记", "否")
        if not run.get("确认时间"):
            run["确认时间"] = adopt_time

        # 并发/迟到的其他成功回执一律搁置，保证唯一有效产物
        for other in self._runs(decision_no):
            if other.get("运行回执编号") == receipt_no:
                continue
            if other.get("确认状态") in (ST_PENDING, ST_CONFIRMED):
                other["确认状态"] = ST_SUPERSEDED
                other["status"] = ST_SUPERSEDED
                other["pending"] = False
                other["迟到标记"] = "是"

        chain = self._chain_of(decision)
        parser = str(run.get("实际解析器"))
        fallback_count = chain.index(parser) if parser in chain else self._next_run_index(decision_no) - 1
        decision["命中解析器"] = parser
        decision["回退次数"] = str(fallback_count)
        decision["决策时间"] = adopt_time
        decision["status"] = "已回退" if fallback_count > 0 else "已选定"
        decision["pending"] = False
        decision["abnormal"] = fallback_count > 0
        primary = chain[0] if chain else parser
        if fallback_count > 0:
            decision["路由依据"] = (
                f"首选 {primary} 失败后按运行台账回退 {fallback_count} 次，"
                f"凭回执 {receipt_no} 采用 {parser} 的已确认字段"
            )
        else:
            decision["路由依据"] = f"首选 {parser} 凭回执 {receipt_no} 确认命中，无需回退"
        return True, (
            f"决策 {decision_no} 已采用回执 {receipt_no}：实际解析器 {parser}，"
            f"回退 {fallback_count} 次，该回执为唯一有效产物"
        ), dict(run)

    def reconcile_decision(self, decision_no: str) -> tuple[bool, str, dict[str, Any] | None]:
        """恢复时服务端先核对已执行解析器与迟到结果，再返回可追溯回执视图。

        界面据此分开「已产出待核对」与「尚未执行」的备选，值班员凭回执继续合法尝试。
        """
        decision = self._find(decision_no)
        if decision is None:
            return False, f"路由决策 {decision_no} 不存在", None

        chain = self._chain_of(decision)
        runs = sorted(self._runs(decision_no), key=lambda r: int(r.get("id", 0)))
        executed_parsers = {str(r.get("实际解析器")) for r in runs}

        pending_review = [
            dict(r) for r in runs if r.get("确认状态") in (ST_PENDING, ST_CONFIRMED)
        ]
        failed_runs = [dict(r) for r in runs if r.get("确认状态") == ST_FAILED]
        adopted = self._adopted_run(decision_no)
        not_started = [parser for parser in chain if parser not in executed_parsers]

        view = {
            "决策编号": decision_no,
            "关联批次": decision.get("关联批次"),
            "推断格式": decision.get("推断格式"),
            "配置解析器链": chain,
            "决策状态": decision.get("status"),
            "已采用回执": dict(adopted) if adopted else None,
            "已产出待核对": pending_review,
            "失败尝试": failed_runs,
            "尚未执行备选": not_started,
            "全部运行回执": [dict(r) for r in runs],
            "台账回退次数": self.ledger_fallback_count(decision_no),
            "可继续尝试": adopted is None and bool(not_started or pending_review),
        }
        message = (
            f"决策 {decision_no}：待核对 {len(pending_review)} 条、"
            f"失败 {len(failed_runs)} 条、尚未执行 {len(not_started)} 个备选"
        )
        if adopted:
            message += f"，已采用回执 {adopted.get('运行回执编号')}"
        return True, message, view

    # ------------------------------------------------------------------
    # 兼容旧调用：同步执行 + 自动落台账
    # ------------------------------------------------------------------
    def run_with_fallback(self, decision_no: str,
                          try_parse: Callable[[str], bool],
                          decide_time: str) -> tuple[bool, str, dict[str, Any] | None]:
        """按解析器链顺序执行，首选失败时回退；每次尝试同步写入持久化运行台账。"""
        decision = self._find(decision_no)
        if decision is None:
            return False, f"路由决策 {decision_no} 不存在", None
        if decision.get("status") == "路由失败" and not self._runs(decision_no):
            return False, (
                f"决策 {decision_no} 已判定路由失败，无解析器可执行"
            ), dict(decision)

        candidates = self._chain_of(decision)
        if not candidates:
            decision["status"] = "路由失败"
            decision["abnormal"] = True
            decision["路由依据"] = "无首选也无备选解析器"
            return False, f"决策 {decision_no} 没有可用解析器", dict(decision)

        # 已持有成功凭据：恢复/重复调用直接回放水化结论，不再执行任何解析器
        adopted = self._adopted_run(decision_no)
        if adopted is not None:
            parser = str(adopted.get("实际解析器"))
            index = candidates.index(parser) if parser in candidates else 0
            decision["命中解析器"] = parser
            decision["回退次数"] = str(index)
            decision["status"] = "已回退" if index > 0 else "已选定"
            decision["abnormal"] = index > 0
            return True, (
                f"决策 {decision_no} 已凭回执 {adopted.get('运行回执编号')} 采用 "
                f"{parser}，恢复时不重复执行、不累加回退次数"
            ), dict(decision)

        primary = candidates[0]
        for index, parser in enumerate(candidates):
            credential = f"digest:run-with-fallback:{decision_no}:{parser}#attempt-{index}"
            ok = bool(try_parse(parser))
            submitted, _msg, run = self.submit_attempt(
                decision_no, parser, credential, success=ok,
                output_fields="" if not ok else f"fields-of-{parser}",
                row_count=0, attempt_time=decide_time,
                failure_reason="" if ok else f"{parser} 解析失败",
            )
            if not submitted:
                return False, _msg, dict(decision)
            if ok:
                self.confirm_run(str(run["运行回执编号"]), decide_time)
                self.adopt_run(decision_no, str(run["运行回执编号"]), decide_time)
                if index > 0:
                    last_message = (
                        f"决策 {decision_no} 回退 {index} 次后由 {parser} 解析成功"
                    )
                else:
                    last_message = f"决策 {decision_no} 首选 {parser} 命中，无需回退"
                return True, last_message, dict(self._find(decision_no))

        decision["回退次数"] = str(len(candidates))
        decision["status"] = "路由失败"
        decision["pending"] = False
        decision["abnormal"] = True
        decision["路由依据"] = f"全部 {len(candidates)} 个解析器均失败（无成功凭据，批次不得写成成功）"
        decision["决策时间"] = decide_time
        return False, (
            f"决策 {decision_no} 的 {len(candidates)} 个解析器全部失败，判定路由失败"
        ), dict(decision)

    # ------------------------------------------------------------------
    # 台账结算：回退次数与指标的唯一依据
    # ------------------------------------------------------------------
    def ledger_fallback_count(self, decision_no: str) -> int:
        """按实际尝试与当次结果核对回退次数：以已采用回执在链中的序号为准。

        没有成功凭据（已采用回执）的决策回退次数按已执行尝试数计，且不得视为成功。
        """
        decision = self._find(decision_no)
        if decision is None:
            return 0
        adopted = self._adopted_run(decision_no)
        if adopted is not None:
            chain = self._chain_of(decision)
            parser = str(adopted.get("实际解析器"))
            if parser in chain:
                return chain.index(parser)
            return int(adopted.get("回退序号") or 0)
        executed = [r for r in self._runs(decision_no) if r.get("确认状态") in EXECUTED_STATES]
        return len(executed)

    def fallback_counts(self) -> tuple[int, int]:
        """返回 (发生回退的决策数, 决策总数)，解析指标据此计算真实回退率。

        有台账的决策按持久化运行记录结算；历史决策保留原回退次数字段口径。
        """
        rows = store.rows(self.MODULE)
        fallbacks = 0
        for row in rows:
            decision_no = str(row.get("决策编号"))
            if self._runs(decision_no):
                count = self.ledger_fallback_count(decision_no)
            else:
                text = str(row.get("回退次数") or "0").strip()
                count = int(text) if text.isdigit() else 0
            if count > 0:
                fallbacks += 1
        return fallbacks, len(rows)

    def successful_decisions(self) -> set[str]:
        """输出持有成功凭据（已采用回执）的决策编号集合。"""
        return {
            str(r.get("决策编号")) for r in store.rows(RUN_MODULE)
            if r.get("确认状态") == ST_ADOPTED
        }

    def failed_route_batches(self) -> list[str]:
        """输出路由失败的投递批次，供解析水位登记滞后原因。"""
        return sorted({
            str(r.get("关联批次")) for r in store.rows(self.MODULE)
            if r.get("status") == "路由失败"
        })
