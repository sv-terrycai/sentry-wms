"""Inventory ledger writer: one row per on-hand change, every entry point.

Convention (kept identical across paths so a single query explains stock):
  INBOUND  +  stock arriving from outside (PO receipt, RMA restock, TO receipt)
  INBOUND  -  reversal of an inbound (void receipt)
  RELEASE  +  stock returning to the shelf (cancel restore, pick release)
  TRANSFER +  destination bin of a bin move; TRANSFER - is its source bin
  SHIPMENT -  outbound pick (written by the pick paths)

external_id is a UUID5 of a caller-supplied key, and inserts are
ON CONFLICT DO NOTHING, so a retried request cannot write a second row.
"""
import uuid

from sqlalchemy import text

from constants import ADJ_APPROVED


def write_adjustment(db, *, item_id, bin_id, warehouse_id, quantity_change,
                     reason_code, key, reason_detail, username,
                     status=ADJ_APPROVED):
    """Insert one ledger row. Returns adjustment_id or None when deduped."""
    if not quantity_change:
        return None
    ext = str(uuid.uuid5(uuid.NAMESPACE_URL, "sentry:adjustment:%s" % key))
    row = db.execute(
        text(
            """
            INSERT INTO inventory_adjustments
                (item_id, bin_id, warehouse_id, quantity_change, reason_code,
                 reason_detail, status, adjusted_by, external_id)
            VALUES (:iid, :bid, :wid, :qty, :reason, :detail, :status,
                    :user, :ext)
            ON CONFLICT (external_id) DO NOTHING
            RETURNING adjustment_id
            """
        ),
        {"iid": item_id, "bid": bin_id, "wid": warehouse_id,
         "qty": int(quantity_change), "reason": reason_code,
         "detail": (reason_detail or "")[:500], "status": status,
         "user": username or "system", "ext": ext},
    ).fetchone()
    if row is None:
        return None
    try:
        return row.adjustment_id
    except AttributeError:
        return row["adjustment_id"]


def record_bin_move(db, *, item_id, from_bin_id, to_bin_id, warehouse_id,
                    quantity, key, username, reason_detail=None):
    """Two TRANSFER rows for one bin move: source negative, destination positive.

    Total stock is unchanged, but reconciliation is per item+bin, so a move
    without these rows leaves both bins un-explainable from the ledger.
    """
    detail = reason_detail or "bin move %s -> %s" % (from_bin_id, to_bin_id)
    write_adjustment(db, item_id=item_id, bin_id=from_bin_id,
                     warehouse_id=warehouse_id,
                     quantity_change=-abs(int(quantity)),
                     reason_code="TRANSFER", key="%s:from" % key,
                     reason_detail=detail, username=username)
    write_adjustment(db, item_id=item_id, bin_id=to_bin_id,
                     warehouse_id=warehouse_id,
                     quantity_change=abs(int(quantity)),
                     reason_code="TRANSFER", key="%s:to" % key,
                     reason_detail=detail, username=username)
