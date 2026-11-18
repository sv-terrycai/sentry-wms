#!/usr/bin/env python3
"""Backfill ledger rows for historical receipts (no inventory change).

Receipts that landed before inventory_adjustments existed have on-hand but no
ledger row, so item+bin cannot be reconciled. This writes CORRECTION rows
that reference the receipt's external_id / PO number. It NEVER touches
inventory.quantity_on_hand.

Idempotency matches the live code: external_id =
UUID5("sentry:adjustment:receipt:<receipt external_id>") - the same key the
receive path writes - so a backfilled receipt can never be double-counted
when the same receipt is seen again.

Default is dry-run. --live requires an explicit flag and prints the rows it
wrote. Run inside the api container:
    python tools/backfill_receipt_ledger.py            # dry-run
    python tools/backfill_receipt_ledger.py --live      # only after approval
"""
import argparse
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import create_engine, text  # noqa: E402


def key_for(receipt_ext):
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          "sentry:adjustment:receipt:%s" % receipt_ext))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="actually insert; default is dry-run")
    args = ap.parse_args(argv)

    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT r.receipt_id, r.external_id, r.item_id, r.bin_id,
                   r.warehouse_id, r.quantity_received,
                   (SELECT po_number FROM purchase_orders p
                     WHERE p.po_id = r.po_id) AS po_number,
                   r.po_id, r.so_id
              FROM item_receipts r
             ORDER BY r.receipt_id
        """)).fetchall()

        # Filter in Python so the UUID5 matches the app byte-for-byte.
        existing = {r[0] for r in conn.execute(
            text("SELECT external_id::text FROM inventory_adjustments")).fetchall()}
        # Test-fixture stock is deliberately excluded: it is not business
        # history, and the self-check whitelists it with its known diff.
        test_items = {r[0] for r in conn.execute(text(
            "SELECT item_id FROM items WHERE sku = 'BRIDGE-P3-TEST'")).fetchall()}
        planned = [r for r in rows
                   if key_for(str(r.external_id)) not in existing
                   and r.item_id not in test_items]

        units = sum(int(r.quantity_received) for r in planned)
        print("planned_rows=%d planned_units=%d" % (len(planned), units))
        for r in planned:
            print("  receipt %s item %s bin %s +%s po=%s"
                  % (r.receipt_id, r.item_id, r.bin_id,
                     r.quantity_received, r.po_number))

        # Bin moves: the physical relocation happened (bin_transfers rows
        # exist) but the ledger has no pair. Backfill from that record, never
        # invented.
        transfers = conn.execute(text("""
            SELECT bt.transfer_id, bt.external_id, bt.item_id,
                   bt.from_bin_id, bt.to_bin_id, bt.warehouse_id,
                   bt.quantity, bt.transfer_type
              FROM bin_transfers bt
             ORDER BY bt.transfer_id
        """)).fetchall()
        needed_keys = {key_for(str(t.external_id)) for t in transfers}
        existing_keys = existing | {str(r.external_id) for r in planned}
        planned_moves = [t for t in transfers
                         if key_for(str(t.external_id)) not in existing_keys
                         and t.item_id not in test_items]
        _ = needed_keys
        print("planned_move_rows=%d planned_move_units=%d"
              % (len(planned_moves) * 2,
                 sum(int(t.quantity) for t in planned_moves)))
        for t in planned_moves:
            print("  transfer %s item %s %s -> %s qty %s (%s)"
                  % (t.transfer_id, t.item_id, t.from_bin_id, t.to_bin_id,
                     t.quantity, t.transfer_type))

        # Diff before/after for the affected item+bins.
        def diff(extra):
            d = conn.execute(text("""
                SELECT inv.item_id, inv.bin_id,
                       SUM(inv.quantity_on_hand) AS on_hand,
                       (SELECT COALESCE(SUM(a.quantity_change), 0)
                          FROM inventory_adjustments a
                         WHERE a.status = 'APPROVED'
                           AND (a.item_id, a.bin_id, a.warehouse_id) IN (
                               SELECT i2.item_id, i2.bin_id, 1
                                 FROM inventory i2
                                WHERE i2.item_id = inv.item_id
                                  AND i2.bin_id = inv.bin_id)) AS ledger
                  FROM inventory inv
                 WHERE inv.warehouse_id = 1
                 GROUP BY inv.item_id, inv.bin_id
            """)).fetchall()
            out = []
            for row in d:
                add = extra.get((row.item_id, row.bin_id), 0)
                delta = int(row.on_hand) - (int(row.ledger) + add)
                if delta:
                    out.append((row.item_id, row.bin_id, delta))
            return out

        planned_by_key = {}
        for r in planned:
            k = (r.item_id, r.bin_id)
            planned_by_key[k] = planned_by_key.get(k, 0) + int(r.quantity_received)
        for t in planned_moves:
            planned_by_key[(t.item_id, t.from_bin_id)] = \
                planned_by_key.get((t.item_id, t.from_bin_id), 0) - int(t.quantity)
            planned_by_key[(t.item_id, t.to_bin_id)] = \
                planned_by_key.get((t.item_id, t.to_bin_id), 0) + int(t.quantity)

        before = diff({})
        after = diff(planned_by_key)
        print("wh01_diff_rows_before=%d units_before=%d"
              % (len(before), sum(x[2] for x in before)))
        print("wh01_diff_rows_after=%d units_after=%d"
              % (len(after), sum(x[2] for x in after)))
        for row in after:
            print("  remaining diff item %s bin %s: %s" % row)

        if not args.live:
            print("dry-run: nothing written")
            return 0

        written = 0
        for r in planned:
            detail = "backfill from receipt %s" % r.external_id
            detail += (" PO %s" % r.po_number) if r.po_number \
                else (" RMA SO %s" % r.so_id)
            res = conn.execute(text("""
                INSERT INTO inventory_adjustments
                    (item_id, bin_id, warehouse_id, quantity_change,
                     reason_code, reason_detail, status, adjusted_by,
                     external_id)
                VALUES (:iid, :bid, :wid, :qty, 'CORRECTION', :detail,
                        'APPROVED', 'ledger-backfill', :ext)
                ON CONFLICT (external_id) DO NOTHING
                RETURNING adjustment_id
            """), {"iid": r.item_id, "bid": r.bin_id, "wid": r.warehouse_id,
                   "qty": int(r.quantity_received), "detail": detail,
                   "ext": key_for(str(r.external_id))}).fetchone()
            if res is not None:
                written += 1
        for t in planned_moves:
            for side, bin_id, delta in (
                    ("from", t.from_bin_id, -int(t.quantity)),
                    ("to", t.to_bin_id, int(t.quantity))):
                res = conn.execute(text("""
                    INSERT INTO inventory_adjustments
                        (item_id, bin_id, warehouse_id, quantity_change,
                         reason_code, reason_detail, status, adjusted_by,
                         external_id)
                    VALUES (:iid, :bid, :wid, :qty, 'TRANSFER', :detail,
                            'APPROVED', 'ledger-backfill', :ext)
                    ON CONFLICT (external_id) DO NOTHING
                    RETURNING adjustment_id
                """), {"iid": t.item_id, "bid": bin_id,
                           "wid": t.warehouse_id, "qty": delta,
                           "detail": "backfill %s transfer %s"
                                     % (t.transfer_type, t.transfer_id),
                           "ext": key_for("%s:backfill-move:%s:%s"
                                          % (t.external_id, t.transfer_id, side))}
                ).fetchone()
                if res is not None:
                    written += 1
        print("written=%d" % written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
