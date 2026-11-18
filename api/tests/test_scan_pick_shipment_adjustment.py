"""Scan-pick (confirm_pick) must write the same SHIPMENT audit row as the
admin virtual pick, so every outbound on-hand deduction is readable from
inventory_adjustments.

Coverage: standard SO pick, wave pick across several SOs, retry, and
transfer-order picks (which do NOT touch inventory or write a row).
"""

import uuid

from db_test_context import get_raw_connection


def _rows(item_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT adjustment_id, quantity_change, reason_code, reason_detail, "
        "       status, adjusted_by FROM inventory_adjustments "
        " WHERE item_id = %s AND reason_code = 'SHIPMENT' "
        " ORDER BY adjustment_id",
        (item_id,),
    )
    rows = cur.fetchall()
    cur.close()
    return rows


def _create_extra_so(so_number, items_qty, warehouse_id=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO sales_orders (so_number, so_barcode, customer_name,
                                     status, warehouse_id, created_by, external_id)
           VALUES (%s, %s, %s, 'OPEN', %s, 'admin', gen_random_uuid())
           RETURNING so_id""",
        (so_number, so_number, "Cust", warehouse_id),
    )
    so_id = cur.fetchone()[0]
    for idx, (item_id, qty) in enumerate(items_qty, 1):
        cur.execute(
            """INSERT INTO sales_order_lines (so_id, item_id, quantity_ordered,
                                              line_number)
               VALUES (%s, %s, %s, %s)""",
            (so_id, item_id, qty, idx),
        )
    cur.close()
    return so_id


def _so_number(so_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT so_number FROM sales_orders WHERE so_id = %s", (so_id,))
    val = cur.fetchone()[0]
    cur.close()
    return val


class TestScanPickShipmentAdjustment:
    def test_standard_pick_writes_one_negative_row(self, client, auth_headers):
        so_id = _create_extra_so("SO-SCAN-%s" % uuid.uuid4().hex[:6], [(1, 2)])
        resp = client.post(
            "/api/picking/create-batch",
            json={"so_identifiers": [_so_number(so_id)], "warehouse_id": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        batch_id = resp.get_json()["batch_id"]
        task = client.get(f"/api/picking/batch/{batch_id}/next",
                          headers=auth_headers).get_json()

        confirm = client.post(
            "/api/picking/confirm",
            json={"pick_task_id": task["pick_task_id"],
                  "scanned_barcode": task["upc"],
                  "quantity_picked": task["quantity_to_pick"]},
            headers=auth_headers,
        )
        assert confirm.status_code == 200, confirm.get_json()

        rows = _rows(1)
        mine = [r for r in rows if "SO-SCAN-" in (r[3] or "")]
        assert len(mine) == 1
        change, reason, detail, status, who = (
            mine[0][1], mine[0][2], mine[0][3], mine[0][4], mine[0][5])
        assert change == -task["quantity_to_pick"]
        assert reason == "SHIPMENT"
        assert "pick_task %s" % task["pick_task_id"] in detail
        assert status == "APPROVED" and who

        # Retry of a picked task is refused and must not add a row.
        again = client.post(
            "/api/picking/confirm",
            json={"pick_task_id": task["pick_task_id"],
                  "scanned_barcode": task["upc"],
                  "quantity_picked": task["quantity_to_pick"]},
            headers=auth_headers,
        )
        assert again.status_code in (400, 409)
        assert len([r for r in _rows(1) if "SO-SCAN-" in (r[3] or "")]) == 1

    def test_wave_pick_writes_one_row_per_contributing_so(self, client,
                                                         auth_headers):
        tag = uuid.uuid4().hex[:6]
        so_a = _create_extra_so("SO-WAVE-A-%s" % tag, [(1, 2)])
        so_b = _create_extra_so("SO-WAVE-B-%s" % tag, [(1, 3)])
        resp = client.post(
            "/api/picking/wave-create",
            json={"so_ids": [so_a, so_b], "warehouse_id": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        batch_id = resp.get_json()["batch_id"]
        tasks = client.get(f"/api/picking/batch/{batch_id}",
                           headers=auth_headers).get_json()["tasks"]
        task = [t for t in tasks if t["sku"] == "TST-001"][0]

        confirm = client.post(
            "/api/picking/confirm",
            json={"pick_task_id": task["pick_task_id"],
                  "scanned_barcode": "100000000001",
                  "quantity_picked": 5},
            headers=auth_headers,
        )
        assert confirm.status_code == 200, confirm.get_json()

        mine = [r for r in _rows(1) if tag in (r[3] or "")]
        assert len(mine) == 2
        by_so = {}
        for r in mine:
            for prefix in ("SO-WAVE-A", "SO-WAVE-B"):
                if prefix in r[3]:
                    by_so[prefix] = r[1]
        assert by_so == {"SO-WAVE-A": -2, "SO-WAVE-B": -3}

    def test_transfer_order_pick_writes_no_shipment_row(self, client,
                                                        auth_headers):
        # TO picks do not move inventory at pick time, so they must not
        # write a SHIPMENT adjustment either.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO transfer_orders (to_number, source_warehouse_id,
                                            destination_warehouse_id, status,
                                            created_by, external_id)
               VALUES (%s, 1, 2, 'OPEN', 'admin', gen_random_uuid())
               RETURNING to_id""",
            ("TO-ADJ-%s" % uuid.uuid4().hex[:6],),
        )
        to_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO transfer_order_lines
                 (to_id, item_id, line_number, requested_qty, committed_qty)
               VALUES (%s, 1, 1, 2, 2) RETURNING to_line_id""",
            (to_id,),
        )
        to_line_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO pick_batches (batch_number, warehouse_id, assigned_to,
                                         status, started_at, completed_at)
               VALUES (%s, 1, 'admin', 'COMPLETED', NOW(), NOW())
               RETURNING batch_id""",
            ("TO-BATCH-%s" % uuid.uuid4().hex[:6],),
        )
        batch_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO pick_tasks (batch_id, to_id, to_line_id, item_id,
                                       bin_id, quantity_to_pick, pick_sequence,
                                       status)
               VALUES (%s, %s, %s, 1, 3, 2, 1, 'PENDING')
               RETURNING pick_task_id""",
            (batch_id, to_id, to_line_id),
        )
        tid = cur.fetchone()[0]
        cur.close()

        before = len(_rows(1))
        resp = client.post(
            "/api/picking/confirm",
            json={"pick_task_id": tid, "scanned_barcode": "100000000001",
                  "quantity_picked": 2},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        assert len(_rows(1)) == before
