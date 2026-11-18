"""Inventory API availability semantics.

Available is a per-bin number: on_hand - allocated from the same row. The
item-level commitment never subtracts into it, and picked units are reported
separately because on_hand already dropped when they were picked.

Regression: RECV-01 on SKU 810199503806 showed Available -156 because the
page subtracted the item-level commitment from a zero-stock bin row.
"""

import uuid

from db_test_context import get_raw_connection


def _seed(item_qty_allocated, picked=0, second_bin=True, status="OPEN"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO items (sku, item_name, upc, external_id) "
        "VALUES (%s, 'OP18', '810199503806', %s) RETURNING item_id",
        ("SKU-INV-%s" % uuid.uuid4().hex[:8], str(uuid.uuid4())))
    item_id = cur.fetchone()[0]
    cur.execute(
        """INSERT INTO inventory (item_id, bin_id, warehouse_id,
                                  quantity_on_hand, quantity_allocated)
           VALUES (%s, 3, 1, 228, 156)""", (item_id,))
    if second_bin:
        cur.execute(
            """INSERT INTO inventory (item_id, bin_id, warehouse_id,
                                      quantity_on_hand, quantity_allocated)
               VALUES (%s, 1, 1, 0, 0)""", (item_id,))
    cur.execute(
        """INSERT INTO sales_orders (so_number, customer_name, status,
                                     warehouse_id, created_by, external_id)
           VALUES (%s, 'Cust', %s, 1, 'admin', gen_random_uuid())
           RETURNING so_id""", ("SO-INV-%s" % uuid.uuid4().hex[:6], status))
    so_id = cur.fetchone()[0]
    cur.execute(
        """INSERT INTO sales_order_lines
             (so_id, item_id, quantity_ordered, quantity_allocated,
              quantity_picked, line_number)
           VALUES (%s, %s, 156, %s, %s, 1)""",
        (so_id, item_id, item_qty_allocated, picked))
    cur.close()
    return item_id, so_id


def _rows(client, auth_headers, item_id):
    resp = client.get(f"/api/admin/inventory?item_id={item_id}&warehouse_id=1",
                      headers=auth_headers)
    assert resp.status_code == 200, resp.get_json()
    return {r["bin_id"]: r for r in resp.get_json()["inventory"]}


def test_empty_bin_available_is_zero_not_negative(client, auth_headers):
    item_id, _ = _seed(item_qty_allocated=156)
    rows = _rows(client, auth_headers, item_id)

    bulk = rows[3]
    assert (bulk["quantity_on_hand"], bulk["quantity_allocated"],
            bulk["quantity_available"]) == (228, 156, 72)

    recv = rows[1]
    assert (recv["quantity_on_hand"], recv["quantity_allocated"],
            recv["quantity_available"]) == (0, 0, 0)
    for r in rows.values():
        assert r["quantity_available"] >= 0


def test_item_commitment_excludes_picked_units(client, auth_headers):
    # 156 allocated of which 56 picked: still-on-shelf commitment is 100,
    # and the 56 picked units are reported separately (on_hand already
    # dropped for them).
    item_id, _ = _seed(item_qty_allocated=156, picked=56, status="PICKED")
    rows = _rows(client, auth_headers, item_id)
    bulk = rows[3]
    assert bulk["committed_to_orders"] == 100
    assert bulk["picked_not_shipped"] == 56


def test_committed_is_item_level_not_bin_level(client, auth_headers):
    item_id, _ = _seed(item_qty_allocated=156)
    rows = _rows(client, auth_headers, item_id)
    # Both bin rows carry the same item-level commitment; neither subtracts
    # it from its own availability.
    assert rows[3]["committed_to_orders"] == 156
    assert rows[1]["committed_to_orders"] == 156
    assert rows[1]["quantity_available"] == 0
