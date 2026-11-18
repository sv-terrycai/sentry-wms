#!/usr/bin/env python3
"""Daily ledger self-check: on_hand must equal the sum of its ledger rows.

Compares every (warehouse, item, bin) with inventory against
inventory_adjustments (status APPROVED). Any non-zero difference is written
to a CSV and, when differences exist, an ALERT file is written. Read-only.

Known whitelist: BRIDGE-P3-TEST (a test fixture whose stock movements
predate the ledger). It is reported with its known diff and reason, and does
not fail the check on its own.

Exit code: 0 when only whitelisted diffs remain, 1 otherwise.

Run (inside the api container or any host with DATABASE_URL):
    DATABASE_URL=... python tools/inventory_ledger_check.py --out-dir /tmp/ledger
"""
import argparse
import csv
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import create_engine, text  # noqa: E402

WHITELIST_SKU = {"BRIDGE-P3-TEST": "test fixture; stock moves predate the ledger"}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="out/ledger_check")
    ap.add_argument("--no-alert-file", action="store_true")
    args = ap.parse_args(argv)

    os.makedirs(args.out_dir, exist_ok=True)
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.begin() as conn:
        rows = conn.execute(text("""
            WITH inv AS (
                SELECT warehouse_id, item_id, bin_id,
                       SUM(quantity_on_hand) AS on_hand,
                       SUM(quantity_allocated) AS allocated
                  FROM inventory
                 GROUP BY warehouse_id, item_id, bin_id
            ), led AS (
                SELECT warehouse_id, item_id, bin_id,
                       SUM(quantity_change) AS ledger
                  FROM inventory_adjustments
                 WHERE status = 'APPROVED'
                 GROUP BY warehouse_id, item_id, bin_id
            )
            SELECT i.warehouse_id, i.item_id, i.bin_id, i.on_hand, i.allocated,
                   COALESCE(l.ledger, 0) AS ledger,
                   i.on_hand - COALESCE(l.ledger, 0) AS diff,
                   it.sku, it.item_name
              FROM inv i
              JOIN items it ON it.item_id = i.item_id
              LEFT JOIN led l
                ON l.warehouse_id = i.warehouse_id
               AND l.item_id = i.item_id
               AND l.bin_id = i.bin_id
             WHERE i.on_hand <> COALESCE(l.ledger, 0)
             ORDER BY i.warehouse_id, abs(i.on_hand - COALESCE(l.ledger, 0)) DESC
        """)).fetchall()

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    path = os.path.join(args.out_dir, "ledger_diff-%s.csv" % stamp)
    hard, whitelisted = [], []
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["warehouse_id", "item_id", "sku", "item_name", "bin_id",
                    "on_hand", "ledger", "diff", "status", "note"])
        for r in rows:
            note = WHITELIST_SKU.get(r.sku)
            status = "whitelisted" if note else "MISMATCH"
            (whitelisted if note else hard).append(r)
            w.writerow([r.warehouse_id, r.item_id, r.sku, r.item_name,
                        r.bin_id, r.on_hand, r.ledger, r.diff, status,
                        note or ""])

    print("rows_compared_diffs=%d hard=%d whitelisted=%d"
          % (len(rows), len(hard), len(whitelisted)))
    for r in rows:
        print("  wh=%s item=%s %s bin=%s on_hand=%s ledger=%s diff=%s%s"
              % (r.warehouse_id, r.item_id, r.sku, r.bin_id, r.on_hand,
                 r.ledger, r.diff,
                 "  [whitelisted]" if r.sku in WHITELIST_SKU else ""))
    print("csv=%s" % path)

    alert = os.path.join(args.out_dir, "ALERT-ledger")
    if hard:
        msg = ("ledger mismatch: %d item+bin rows, first: wh=%s item=%s bin=%s diff=%s"
               % (len(hard), hard[0].warehouse_id, hard[0].item_id,
                  hard[0].bin_id, hard[0].diff))
        print("ALERT " + msg)
        if not args.no_alert_file:
            with open(alert, "w", encoding="utf-8") as f:
                f.write(msg + "\n")
        return 1
    if not args.no_alert_file and os.path.exists(alert):
        os.unlink(alert)
    print("ok: every on_hand change is explained by its ledger")
    return 0


if __name__ == "__main__":
    sys.exit(main())
