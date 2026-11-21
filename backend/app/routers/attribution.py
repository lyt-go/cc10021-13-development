"""质量事件归属接口：回填、认领、写回、业务域调整、主数据派发和日报。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.dq_incident_attribution import DqIncidentAttributionService
from app.store import store

router = APIRouter(prefix="/api/dq_incident_attribution", tags=["质量事件归属"])

service = DqIncidentAttributionService()


class ClaimPayload(BaseModel):
    group: str
    # 也可直接按事件编号认领（事件下可能有多条业务域归属记录）。
    attribution_id: int | None = None
    incident_no: str | None = None


class WriteBackPayload(BaseModel):
    group: str
    values: dict[str, Any] = Field(default_factory=dict)


class AdjustPayload(BaseModel):
    new_domain: str
    new_group: str
    operator: str = ""


class ReviewPayload(BaseModel):
    table: str
    column: str
    domain: str = ""
    group: str = ""
    reviewer: str = ""


@router.post("/backfill")
def backfill() -> dict[str, Any]:
    """回填存量事件的归属记录，缺字段映射的进入待复核。"""
    return service.backfill()


@router.get("/claims")
def list_claims() -> dict[str, Any]:
    items = service.list_claims()
    return {"items": items, "total": len(items)}


@router.get("/review-queue")
def review_queue() -> dict[str, Any]:
    items = service.review_queue()
    return {"items": items, "total": len(items)}


@router.get("/crosscheck")
def crosscheck(group: str | None = Query(default=None)) -> dict[str, Any]:
    items = service.crosscheck(group)
    return {"items": items, "total": len(items)}


@router.get("/dispatch")
def dispatch(group: str = Query(...)) -> dict[str, Any]:
    """主数据校验派发：某责任组可见字段和可认领处置项。"""
    return service.dispatch_mdm_validation(group)


@router.get("/report")
def report(group: str | None = Query(default=None)) -> dict[str, Any]:
    if group:
        return service.group_report(group)
    return service.build_daily_report()


@router.post("/{attribution_id}/claim")
def claim(attribution_id: int, payload: ClaimPayload) -> dict[str, Any]:
    """按归属记录认领；不传归属编号时按事件编号 + 责任组定位。"""
    target_id = attribution_id
    if target_id <= 0 and payload.incident_no:
        target = next(
            (
                row for row in store.rows(service.MODULE)
                if row.get("事件编号") == payload.incident_no
                and row.get("责任组") == payload.group.strip()
            ),
            None,
        )
        if target is None:
            raise HTTPException(
                status_code=404,
                detail=f"{payload.group} 在事件 {payload.incident_no} 上没有可认领的归属记录",
            )
        target_id = int(target["id"])
    return service.claim(target_id, payload.group)


@router.post("/{attribution_id}/write-back")
def write_back(attribution_id: int, payload: WriteBackPayload) -> dict[str, Any]:
    return service.write_back(attribution_id, payload.group, payload.values)


@router.post("/{attribution_id}/adjust")
def adjust(attribution_id: int, payload: AdjustPayload) -> dict[str, Any]:
    return service.adjust_domain(
        attribution_id, payload.new_domain, payload.new_group, payload.operator)


@router.post("/{attribution_id}/review")
def review(attribution_id: int, payload: ReviewPayload) -> dict[str, Any]:
    return service.resolve_review(
        attribution_id, payload.table, payload.column,
        payload.domain, payload.group, payload.reviewer)


@router.get("/events/{incident_no}/resolve")
def resolve_incident(incident_no: str) -> dict[str, Any]:
    """只读解析事件归属：展示表/字段身份和命中的业务域候选。"""
    result = service.resolve_incident(incident_no)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=f"质量事件 {incident_no} 不存在")
    return result


@router.post("/events/{incident_no}/sync")
def sync_incident(incident_no: str) -> dict[str, Any]:
    result = service.sync_incident(incident_no)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=f"质量事件 {incident_no} 不存在")
    return result
