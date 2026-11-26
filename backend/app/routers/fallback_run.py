"""解析路由回退运行接口：恢复核对、尝试提交、迟到确认、回执采用与指标追溯。

与通用资源路由分开注册，路径固定在 /api/parser-route 与 /api/parse-metric 下，
避免被 /api/{module} 动态路由抢先匹配。业务判断全部落在 ParserRouteService。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services import service_for
from app.store import store

router = APIRouter(prefix="/api", tags=["解析路由回退运行"])


class AttemptPayload(BaseModel):
    parser: str
    input_credential: str
    success: bool
    output_fields: str = ""
    row_count: int = 0
    attempt_time: str
    failure_reason: str = ""


class ConfirmPayload(BaseModel):
    confirm_time: str
    late: bool = False


def _route_service():
    service = service_for("parser_route")
    if service is None:  # pragma: no cover - 注册表恒有该服务
        raise HTTPException(status_code=404, detail="解析路由服务未注册")
    return service


@router.get("/parser-route/runs", response_model=dict)
def list_runs(decision_no: str | None = None) -> dict[str, Any]:
    """运行台账：可按决策编号过滤，指标复核页据此回到实际执行记录。"""
    rows = store.rows("fallback_run")
    if decision_no:
        rows = [r for r in rows if r.get("决策编号") == decision_no]
    return {"items": [dict(r) for r in rows], "total": len(rows)}


@router.get("/parser-route/decisions/{decision_no}/reconcile", response_model=ActionResult)
def reconcile(decision_no: str) -> ActionResult:
    """恢复时先核对已执行解析器与迟到结果，分开待核对与尚未执行的备选。"""
    service = _route_service()
    ok, message, view = service.reconcile_decision(decision_no)
    if not ok:
        raise HTTPException(status_code=404, detail=message)
    return ActionResult(ok=True, message=message, entry=view)


@router.post("/parser-route/decisions/{decision_no}/attempts", response_model=ActionResult)
def submit_attempt(decision_no: str, payload: AttemptPayload) -> ActionResult:
    """提交一次解析尝试；同解析器同输入凭据重复提交幂等返回原回执。"""
    service = _route_service()
    ok, message, run = service.submit_attempt(
        decision_no, payload.parser, payload.input_credential,
        payload.success, payload.output_fields, payload.row_count,
        payload.attempt_time, payload.failure_reason,
    )
    return ActionResult(ok=ok, message=message, entry=run)


@router.post("/parser-route/runs/{receipt_no}/confirm", response_model=ActionResult)
def confirm_run(receipt_no: str, payload: ConfirmPayload) -> ActionResult:
    """服务端核对解析入口迟到的成功回执，不重新执行解析器。"""
    service = _route_service()
    ok, message, run = service.confirm_run(receipt_no, payload.confirm_time, payload.late)
    return ActionResult(ok=ok, message=message, entry=run)


@router.post("/parser-route/decisions/{decision_no}/adopt/{receipt_no}", response_model=ActionResult)
def adopt_run(decision_no: str, receipt_no: str, payload: ConfirmPayload) -> ActionResult:
    """值班员凭回执采用唯一有效产物，结论同步到路由决策与运行台账。"""
    service = _route_service()
    ok, message, run = service.adopt_run(decision_no, receipt_no, payload.confirm_time)
    return ActionResult(ok=ok, message=message, entry=run)


@router.get("/parse-metric/readings/{reading_no}/trace", response_model=dict)
def trace_reading(reading_no: str) -> dict[str, Any]:
    """指标复核页从采用结果回到对应的实际执行记录。"""
    metric_service = service_for("parse_metric")
    if metric_service is None:  # pragma: no cover
        raise HTTPException(status_code=404, detail="解析指标服务未注册")
    runs = metric_service.trace_runs(reading_no)
    if not runs:
        raise HTTPException(
            status_code=404,
            detail=f"指标读数 {reading_no} 不存在或其来源渠道暂无回退运行记录",
        )
    return {
        "指标读数编号": reading_no,
        "运行回执": runs,
        "total": len(runs),
        "已采用数": sum(1 for r in runs if r.get("确认状态") == "已采用"),
    }
