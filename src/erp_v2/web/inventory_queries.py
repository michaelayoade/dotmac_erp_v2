"""Inventory workspace reads over dotmac-inventory models.

Balances come from the module's stock_balances projection (rebuilt
from movements by the module), movements are the movement ledger.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from dotmac_inventory.models import Item, StockBalance, StockMovement, Warehouse
from sqlalchemy import select
from sqlalchemy.orm import Session


def stock_overview(db: Session, *, tenant_id: UUID, limit: int = 100) -> dict[str, object]:
    items = {i.id: i for i in db.scalars(select(Item).where(Item.tenant_id == tenant_id))}
    warehouses = {w.id: w for w in db.scalars(select(Warehouse).where(Warehouse.tenant_id == tenant_id))}
    balances = db.scalars(
        select(StockBalance).where(StockBalance.tenant_id == tenant_id).order_by(StockBalance.item_id).limit(limit)
    )
    lines = []
    total_value = Decimal(0)
    for b in balances:
        item = items.get(b.item_id)
        warehouse = warehouses.get(b.warehouse_id)
        available = b.quantity_on_hand - b.quantity_reserved
        total_value += b.total_value
        lines.append(
            {
                "sku": item.sku if item else "—",
                "item": item.name if item else "—",
                "warehouse": warehouse.code if warehouse else "—",
                "on_hand": b.quantity_on_hand,
                "reserved": b.quantity_reserved,
                "available": available,
                "unit_cost": b.current_unit_cost,
                "value": b.total_value,
            }
        )
    return {
        "lines": lines,
        "total_value": total_value,
        "warehouses": [{"code": w.code, "name": w.name} for w in warehouses.values()],
        "item_count": len(items),
    }


def recent_movements(db: Session, *, tenant_id: UUID, limit: int = 30) -> list[dict[str, object]]:
    items = {i.id: i.sku for i in db.scalars(select(Item).where(Item.tenant_id == tenant_id))}
    rows = db.scalars(
        select(StockMovement)
        .where(StockMovement.tenant_id == tenant_id)
        .order_by(StockMovement.occurred_at.desc())
        .limit(limit)
    )
    return [
        {
            "sku": items.get(m.item_id, "—"),
            "kind": m.kind,
            "quantity_delta": m.quantity_delta,
            "value_delta": m.value_delta,
            "occurred_at": m.occurred_at,
            "source_ref": m.source_ref,
        }
        for m in rows
    ]


__all__ = ["recent_movements", "stock_overview"]
