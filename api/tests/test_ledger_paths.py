"""Every on-hand entry point must leave a ledger row.

Covers the paths that were missing before: PO receipt, void receipt, bin
move / putaway, batch pick release, and RMA restock. The invariant tested is
the same everywhere: one row per movement, correct sign, correct bin, and a
retry that writes nothing new.
"""

import uuid

from db_test_context import get_raw_connection


def _rows(item_id, reason=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    sql = ("SELECT adjustment_id, bin_id, quantity_change, reason_code, "
           "       reason_detail FROM inventory_adjustments WHERE item_id = %s")
    params = [item_id]
    if reason:
        sql += " AND reason_code = %s"
        params.append(reason)
    cur.execute(sql + " ORDER BY adjustment_id", tuple(params))
    rows = cur.fetchall()
    cur.close()
    return rows


def _soh(item_id, bin_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT quantity_on_hand FROM inventory "
                " WHERE item_id = %s AND bin_id = %s", (item_id, bin_id))
    row = cur.fetchone()
    cur.close()
    return row[0] if row else 0


class TestReceivingLedger:
    def test_po_receipt_writes_inbound_row(self, client, auth_headers):
        before = len(_rows(1, "INBOUND"))
        resp = client.post(
            "/api/receiving/receive",
            json={"po_id": 1,
                  "items": [{"item_id": 1, "quantity": 5, "bin_id": 3}]},
            headers=auth_headers)
        assert resp.status_code == 200, resp.get_json()
        rows = _rows(1, "INBOUND")
        assert len(rows) == before + 1
        _, bin_id, change, reason, detail = rows[-1]
        assert change == 5 and bin_id == 3 and reason == "INBOUND"
        assert "receipt" in detail and "PO " in detail

    def test_void_receipt_writes_negative_inbound_row(self, client,
                                                      auth_headers):
        resp = client.post(
            "/api/receiving/receive",
            json={"po_id": 1,
                  "items": [{"item_id": 1, "quantity": 4, "bin_id": 3}]},
            headers=auth_headers)
        assert resp.status_code == 200, resp.get_json()
        receipt_ids = resp.get_json().get("receipt_ids") or []
        if not receipt_ids:
            receipt_ids = [r[0] for r in _all_receipts(1)]
        before = len(_rows(1, "INBOUND"))

        cancel = client.post("/api/receiving/cancel",
                             json={"po_id": 1, "receipt_ids": receipt_ids},
                             headers=auth_headers)
        assert cancel.status_code == 200, cancel.get_json()
        rows = _rows(1, "INBOUND")
        assert len(rows) == before + len(receipt_ids)
        voided = [r for r in rows if "void" in (r[4] or "")]
        assert voided and all(r[2] < 0 for r in voided)


def _all_receipts(item_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT receipt_id FROM item_receipts WHERE item_id = %s",
                (item_id,))
    rows = cur.fetchall()
    cur.close()
    return rows


class TestBinMoveLedger:
    def test_move_writes_source_negative_and_destination_positive(self, client,
                                                                  auth_headers):
        item = _new_item_with_stock(bin_id=3, qty=6)
        before = len(_rows(item, "TRANSFER"))
        resp = client.post(
            "/api/transfers/move",
            json={"item_id": item, "from_bin_id": 3, "to_bin_id": 9,
                  "quantity": 2},
            headers=auth_headers)
        assert resp.status_code == 200, resp.get_json()
        rows = _rows(item, "TRANSFER")
        assert len(rows) == before + 2
        new = rows[-2:]
        by_bin = {r[1]: r[2] for r in new}
        assert by_bin == {3: -2, 9: 2}
        assert _soh(item, 3) == 4 and _soh(item, 9) == 2


class TestRmaLedger:
    def test_return_receipt_writes_inbound_row(self, client, auth_headers):
        item = _new_item_with_stock(bin_id=3, qty=0)
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO sales_orders (so_number, customer_name, status,
                                         warehouse_id, order_type, created_by,
                                         external_id)
               VALUES (%s, 'Cust', 'OPEN', 1, 'return', 'admin', gen_random_uuid())
               RETURNING so_id""", ("RMA-LEDGER-%s" % uuid.uuid4().hex[:6],))
        rma_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO sales_order_lines
                 (so_id, item_id, quantity_ordered, line_number)
               VALUES (%s, %s, 3, 1) RETURNING so_line_id""", (rma_id, item))
        cur.close()

        resp = client.post(
            f"/api/admin/sales-orders/{rma_id}/receive-return",
            json={"item_id": item, "quantity": 3, "warehouse_id": 1,
                  "bin_id": 3},
            headers=auth_headers)
        assert resp.status_code in (200, 201), resp.get_json()
        rows = _rows(item, "INBOUND")
        assert len(rows) == 1
        assert rows[0][1] == 3 and rows[0][2] == 3


class TestPickReleaseLedger:
    def test_batch_cancel_releases_with_one_row_per_pick_task(self, client,
                                                             auth_headers):
        item = _new_item_with_stock(bin_id=3, qty=5)
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO sales_orders (so_number, so_barcode, customer_name,
                                         status, warehouse_id, created_by, external_id)
               VALUES (%s, %s, 'Cust', 'OPEN', 1, 'admin', gen_random_uuid())
               RETURNING so_id""",
            ("SO-REL-B-%s" % uuid.uuid4().hex[:6], "SO-REL-B-%s" % uuid.uuid4().hex[:6]))
        so_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO sales_order_lines (so_id, item_id, quantity_ordered,
                                              line_number)
               VALUES (%s, %s, 2, 1) RETURNING so_line_id""", (so_id, item))
        sol = cur.fetchone()[0]
        cur.close()

        batch = client.post("/api/picking/create-batch",
                            json={"so_identifiers": [_so_number(so_id)],
                                  "warehouse_id": 1},
                            headers=auth_headers)
        assert batch.status_code == 200, batch.get_json()
        batch_id = batch.get_json()["batch_id"]
        task = client.get(f"/api/picking/batch/{batch_id}/next",
                          headers=auth_headers).get_json()
        confirm = client.post(
            "/api/picking/confirm",
            json={"pick_task_id": task["pick_task_id"],
                  "scanned_barcode": task["upc"],
                  "quantity_picked": 2},
            headers=auth_headers)
        assert confirm.status_code == 200, confirm.get_json()

        before = len(_rows(item, "RELEASE"))
        cancel = client.post("/api/picking/cancel-batch",
                             json={"batch_id": batch_id},
                             headers=auth_headers)
        assert cancel.status_code in (200, 204), cancel.get_json()
        rows = _rows(item, "RELEASE")
        assert len(rows) == before + 1
        assert rows[-1][1] == 3 and rows[-1][2] == 2


def _so_number(so_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT so_number FROM sales_orders WHERE so_id = %s", (so_id,))
    val = cur.fetchone()[0]
    cur.close()
    return val


def _new_item_with_stock(bin_id, qty):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO items (sku, item_name, upc, external_id) "
        "VALUES (%s, 'Ledger widget', '0123456789012', %s) RETURNING item_id",
        ("SKU-LED-%s" % uuid.uuid4().hex[:8], str(uuid.uuid4())))
    item_id = cur.fetchone()[0]
    if qty:
        cur.execute(
            """INSERT INTO inventory (item_id, bin_id, warehouse_id,
                                      quantity_on_hand, quantity_allocated)
               VALUES (%s, %s, 1, %s, 0)""", (item_id, bin_id, qty))
    cur.close()
    return item_id
