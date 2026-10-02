from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.models import Pillar, Segment, Vendor
from app.services.first_fit_engine import free_spans_from_pillars

router = APIRouter(prefix="/vendors", tags=["vendors"])

ACTIVE = "active"
MERGED = "merged"
STATUS_TEXT = {ACTIVE: "有效", MERGED: "已合并", "withdrawn": "已撤出"}


def vendor_dict(r: Vendor) -> dict:
    return {"id": r.id, "market_day_id": r.market_day_id, "name": r.name,
            "stall_width_m": r.stall_width_m, "priority": r.priority, "status": r.status}


@router.get("")
def list_vendors(db: Session = Depends(get_db)):
    return [vendor_dict(r) for r in db.scalars(select(Vendor).order_by(Vendor.priority, Vendor.id)).all()]


class MergeIn(BaseModel):
    vendor_id_a: int
    vendor_id_b: int


def _merged_width_fits(db: Session, market_day_id: int, width: float) -> bool:
    """True if some pillar gap in any segment of the day can hold the merged width."""
    segs = db.scalars(select(Segment).where(Segment.market_day_id == market_day_id)).all()
    for seg in segs:
        pillars = [{"position_m": p.position_m, "thickness_m": p.thickness_m}
                   for p in db.scalars(select(Pillar).where(Pillar.segment_id == seg.id)).all()]
        if any(end - start + 1e-9 >= width
               for start, end in free_spans_from_pillars(seg.width_m, pillars)):
            return True
    return False


@router.post("/merge")
def merge_vendors(body: MergeIn, db: Session = Depends(get_db)):
    """一次提交合并两个仍有效摊主为新占位摊。只写摊主表，零写运行行；确认开间由 /allocate/run 落库。"""
    if body.vendor_id_a == body.vendor_id_b:
        raise HTTPException(400, "不能把摊主和自己合并，请选两个不同摊主")
    a = db.get(Vendor, body.vendor_id_a)
    b = db.get(Vendor, body.vendor_id_b)
    if not a or not b:
        raise HTTPException(404, "摊主不存在")
    inactive = [v for v in (a, b) if v.status != ACTIVE]
    if inactive:
        # 已退出再合成：拒因与"塞不下"互斥，只说是已退出
        desc = "、".join(f"{v.name}（{STATUS_TEXT.get(v.status, v.status)}）" for v in inactive)
        raise HTTPException(409, f"摊主{desc}已退出有效队列，不能再参与合并")
    if a.market_day_id != b.market_day_id:
        raise HTTPException(400, "两位摊主不在同一集日，不能合并")

    width = round(a.stall_width_m + b.stall_width_m, 3)
    priority = min(a.priority, b.priority)  # 数字越小优先级越高，取较高者

    # 合成宽塞不进任一柱间 → 整单失败：不落新摊、原两摊状态与运行行都停在合成前
    if not _merged_width_fits(db, a.market_day_id, width):
        raise HTTPException(422, f"合成后宽度 {width} m 在任一柱间都塞不下，本次合并未生效")

    first, second = sorted((a, b), key=lambda v: (v.priority, v.id))
    merged = Vendor(market_day_id=a.market_day_id,
                    name=f"{first.name}+{second.name}",
                    stall_width_m=width, priority=priority, status=ACTIVE)
    a.status = MERGED
    b.status = MERGED
    db.add(merged)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(merged)
    return {"merged": vendor_dict(merged), "retired_ids": [a.id, b.id]}
