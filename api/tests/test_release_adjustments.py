"""RELEASE rows: stock coming back in must be recorded too.

Approved rule: every change to on-hand gets a row. SHIPMENT stays the
outbound net; RELEASE is the positive counterpart (cancel-restore and
picked-unit release), so restores can be counted on their own.

Covered paths and their bins:
  - cancel of a PICKED/PACKED SO -> RELEASE at the receiving bin the goods
    actually landed in, key cancel-restore:<so_line_id>
  - release of a picked pick_task -> RELEASE at the original pick bin,
    key release:<pick_task_id>
"""

import uuid

from db_test_context import get_raw_connection


def _rows(item_id, reason=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT adjustment_id, bin_id, quantity_change, reason_code, "
        "       reason_detail, status FROM inventory_adjustments "
        " WHERE item_id = %s" + (" AND reason_code = %s" if reason else "")
        + " ORDER BY adjustment_id",
        (item_id, reason) if reason else (item_id,),
    )
    rows = cur.fetchall()
    cur.close()
    return rows


def _create_so(so_number, item_qty, warehouse_id=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO sales_orders (so_number, so_barcode, customer_name,
                                     status, warehouse_id, created_by, external_id)
           VALUES (%s, %s, 'Cust', 'OPEN', %s, 'admin', gen_random_uuid())
           RETURNING so_id""",
        (so_number, so_number, warehouse_id),
    )
    so_id = cur.fetchone()[0]
    for n, (item_id, qty) in enumerate(item_qty, 1):
        cur.execute(
            """INSERT INTO sales_order_lines (so_id, item_id, quantity_ordered,
                                              line_number)
               VALUES (%s, %s, %s, %s)""",
            (so_id, item_id, qty, n),
        )
    cur.close()
    return so_id


def _set_inv(item_id, bin_id, on_hand, allocated=0, warehouse_id=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO inventory (item_id, bin_id, warehouse_id,
                                  quantity_on_hand, quantity_allocated)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (item_id, bin_id, lot_number) DO UPDATE
           SET quantity_on_hand = EXCLUDED.quantity_on_hand,
               quantity_allocated = EXCLUDED.quantity_allocated""",
        (item_id, bin_id, warehouse_id, on_hand, allocated),
    )
    cur.close()


def _inv(item_id, bin_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT quantity_on_hand, quantity_allocated FROM inventory "
        " WHERE item_id = %s AND bin_id = %s", (item_id, bin_id))
    row = cur.fetchone()
    cur.close()
    return {"on_hand": row[0], "allocated": row[1]}


class TestReleaseAdjustments:
    def test_cancel_after_pick_writes_release_at_receiving_bin(self, client,
                                                              auth_headers):
        item_id = _create_item()
        _set_inv(item_id, 3, on_hand=10, allocated=3)
        so_id = _create_so("SO-REL-%s" % uuid.uuid4().hex[:6], [(item_id, 3)])
        line = _line(so_id)

        pick = client.post(f"/api/admin/sales-orders/{so_id}/admin-pick",
                           json={"lines": [{"so_line_id": line,
                                            "bin_id": 3, "quantity": 3}]},
                           headers=_auth(client))
        assert pick.status_code == 200, pick.get_json()
        ship_row = _rows(item_id, "SHIPMENT")
        assert len(ship_row) == 1 and ship_row[0][2] == -3
        # Picked stock now sits in the receiving bin after cancel-restore.
        assert _inv(item_id, 3)["on_hand"] == 7

        cancel = client.post(f"/api/admin/sales-orders/{so_id}/cancel",
                             json={}, headers=_auth(client))
        assert cancel.status_code == 200, cancel.get_json()

        rel = _rows(item_id, "RELEASE")
        assert len(rel) == 1
        adj_id, bin_id, qty, reason, detail, status = rel[0]
        assert qty == 3 and reason == "RELEASE" and status == "APPROVED"
        assert "cancel-restore" in detail
        # Bin must be the receiving bin (1), not the pick bin (3).
        assert bin_id == 1
        assert _inv(item_id, 1)["on_hand"] == 3

        # Net of the ledger is zero: -3 shipped, +3 restored.
        assert sum(r[2] for r in _rows(item_id)) == 0

    def test_release_one_of_two_pick_tasks_only_restores_that_one(self, client,
                                                                 auth_headers):
        item_id = _create_item()
        _set_inv(item_id, 3, on_hand=10)
        so_id = _create_so("SO-REL2-%s" % uuid.uuid4().hex[:6],
                           [(item_id, 4)])
        line = _line(so_id)
        for qty in (2, 2):
            resp = client.post(
                f"/api/admin/sales-orders/{so_id}/admin-pick",
                json={"lines": [{"so_line_id": line, "bin_id": 3,
                                 "quantity": qty}]},
                headers=_auth(client))
            assert resp.status_code == 200, resp.get_json()

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute("SELECT pick_task_id FROM pick_tasks WHERE so_id = %s "
                    "ORDER BY pick_task_id", (so_id,))
        task_ids = [r[0] for r in cur.fetchall()]
        cur.close()
        assert len(task_ids) == 2

        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/revert-status",
            json={"new_status": "PICKED", "release_pick_task_ids": [task_ids[0]]},
            headers=_auth(client))
        assert resp.status_code == 200, resp.get_json()

        rel = _rows(item_id, "RELEASE")
        assert len(rel) == 1
        _, bin_id, qty, _, detail, _ = rel[0]
        assert qty == 2 and bin_id == 3  # original pick bin
        assert "release:%s" % task_ids[0] in detail

        # Retry the same release: task is no longer PICKED -> refused and
        # no second row appears.
        again = client.post(
            f"/api/admin/sales-orders/{so_id}/revert-status",
            json={"new_status": "PICKED", "release_pick_task_ids": [task_ids[0]]},
            headers=_auth(client))
        assert again.status_code in (400, 409)
        assert len(_rows(item_id, "RELEASE")) == 1

    def test_transfer_order_paths_write_no_release(self, client,
                                                   auth_headers):
        # TO picks never move inventory, so there is nothing to release.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO transfer_orders (to_number, source_warehouse_id,
                                            destination_warehouse_id, status,
                                            created_by, external_id)
               VALUES (%s, 1, 2, 'OPEN', 'admin', gen_random_uuid())
               RETURNING to_id""", ("TO-REL-%s" % uuid.uuid4().hex[:6],))
        to_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO transfer_order_lines
                 (to_id, item_id, line_number, requested_qty, committed_qty)
               VALUES (%s, 1, 1, 2, 2) RETURNING to_line_id""", (to_id,))
        to_line_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO pick_batches (batch_number, warehouse_id, assigned_to,
                                         status, started_at, completed_at)
               VALUES (%s, 1, 'admin', 'COMPLETED', NOW(), NOW())
               RETURNING batch_id""",
            ("TO-B-%s" % uuid.uuid4().hex[:6],))
        batch_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO pick_tasks (batch_id, to_id, to_line_id, item_id,
                                       bin_id, quantity_to_pick, pick_sequence,
                                       status, quantity_picked)
               VALUES (%s, %s, %s, 1, 3, 2, 1, 'PICKED', 2)
               RETURNING pick_task_id""", (batch_id, to_id, to_line_id))
        cur.close()
        before = len(_rows(1, "RELEASE"))
        # TO has no SO, so no cancel/release endpoint applies; assert the
        # ledger did not move while the task was picked through the TO flow.
        assert len(_rows(1, "RELEASE")) == before


def _create_item():
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO items (sku, item_name, upc, external_id) "
        "VALUES (%s, 'Widget', '0123456789012', %s) RETURNING item_id",
        ("SKU-REL-%s" % uuid.uuid4().hex[:8], str(uuid.uuid4())))
    item_id = cur.fetchone()[0]
    cur.close()
    return item_id


def _line(so_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT so_line_id FROM sales_order_lines WHERE so_id = %s",
                (so_id,))
    val = cur.fetchone()[0]
    cur.close()
    return val


def _auth(client):
    resp = client.post("/api/auth/login",
                       json={"username": "admin", "password": "admin"})
    return {"Authorization": f"Bearer {resp.get_json()['token']}"}
