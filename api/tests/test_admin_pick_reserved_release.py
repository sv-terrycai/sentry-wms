"""Reservation release on admin pick, and the ship/cancel invariants.

A bridge-created (reserve-at-creation) SO carries a standing reservation
with no pick tasks. Converting reserved units into picked units must move
inventory.quantity_on_hand and inventory.quantity_allocated by the same
amount, leaving available (on_hand - allocated) unchanged. Cancel of an
allocated-but-unpicked SO must still release the whole reservation, which
RAISES available.

Every test asserts the three numbers, not just status.
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db_test_context import get_raw_connection


def _insert_so(status="OPEN", warehouse_id=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_orders "
        "(so_number, customer_name, status, warehouse_id, external_id, order_type) "
        "VALUES (%s, %s, %s, %s, %s, 'sale') RETURNING so_id",
        (f"SO-RESV-{uuid.uuid4().hex[:8]}", "Cust", status, warehouse_id,
         str(uuid.uuid4())),
    )
    so_id = cur.fetchone()[0]
    cur.close()
    return so_id


def _insert_item():
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO items (sku, item_name, upc, external_id) "
        "VALUES (%s, %s, %s, %s) RETURNING item_id",
        (f"SKU-RESV-{uuid.uuid4().hex[:8]}", "Widget", "0123456789012",
         str(uuid.uuid4())),
    )
    item_id = cur.fetchone()[0]
    cur.close()
    return item_id


def _insert_line(so_id, item_id, *, qty_ordered, qty_allocated, qty_picked=0,
                 line_number=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_order_lines "
        "(so_id, item_id, quantity_ordered, quantity_picked, "
        " quantity_allocated, line_number, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'OPEN') RETURNING so_line_id",
        (so_id, item_id, qty_ordered, qty_picked, qty_allocated, line_number),
    )
    sol_id = cur.fetchone()[0]
    cur.close()
    return sol_id


def _set_inv(item_id, bin_id, qty_on_hand, qty_allocated=0, warehouse_id=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO inventory "
        "(item_id, bin_id, warehouse_id, quantity_on_hand, quantity_allocated) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (item_id, bin_id, lot_number) DO UPDATE "
        "SET quantity_on_hand = EXCLUDED.quantity_on_hand, "
        "    quantity_allocated = EXCLUDED.quantity_allocated",
        (item_id, bin_id, warehouse_id, qty_on_hand, qty_allocated),
    )
    cur.close()


def _inv(item_id, bin_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT quantity_on_hand, quantity_allocated, "
        "       (quantity_on_hand - quantity_allocated) AS available "
        "  FROM inventory WHERE item_id = %s AND bin_id = %s",
        (item_id, bin_id),
    )
    row = cur.fetchone()
    cur.close()
    return {"on_hand": row[0], "allocated": row[1], "available": row[2]}


def _line(sol_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT quantity_ordered, quantity_picked, quantity_allocated "
        "  FROM sales_order_lines WHERE so_line_id = %s",
        (sol_id,),
    )
    row = cur.fetchone()
    cur.close()
    return {"ordered": row[0], "picked": row[1], "allocated": row[2]}


def _auth(client):
    resp = client.post("/api/auth/login",
                       json={"username": "admin", "password": "admin"})
    return {"Authorization": f"Bearer {resp.get_json()['token']}"}


class TestAdminPickReleasesReservation:
    def test_single_bin_reserved_pick_keeps_available(self, client):
        # 10 on hand, 4 reserved for the SO; available 6.
        item_id = _insert_item()
        _set_inv(item_id, 3, qty_on_hand=10, qty_allocated=4)
        so_id = _insert_so()
        sol_id = _insert_line(so_id, item_id, qty_ordered=4, qty_allocated=4)

        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/admin-pick",
            json={"lines": [{"so_line_id": sol_id, "bin_id": 3,
                             "quantity": 4}]},
            headers=_auth(client),
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["reservations_released"] == 4

        inv = _inv(item_id, 3)
        assert inv["on_hand"] == 6        # 10 - 4
        assert inv["allocated"] == 0      # 4 - 4
        assert inv["available"] == 6      # unchanged
        line = _line(sol_id)
        assert line["picked"] == 4
        assert line["allocated"] == 4     # picked floor

    def test_no_reservation_keeps_legacy_behaviour(self, client):
        # No standing reservation: original admin-pick path, inventory
        # allocation untouched.
        item_id = _insert_item()
        _set_inv(item_id, 3, qty_on_hand=5, qty_allocated=0)
        so_id = _insert_so()
        sol_id = _insert_line(so_id, item_id, qty_ordered=2, qty_allocated=0)

        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/admin-pick",
            json={"lines": [{"so_line_id": sol_id, "bin_id": 3,
                             "quantity": 2}]},
            headers=_auth(client),
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["reservations_released"] == 0
        inv = _inv(item_id, 3)
        assert inv["on_hand"] == 3
        assert inv["allocated"] == 0

    def test_split_across_bins_releases_reservation_once(self, client):
        item_id = _insert_item()
        _set_inv(item_id, 3, qty_on_hand=5, qty_allocated=2)
        _set_inv(item_id, 4, qty_on_hand=5, qty_allocated=2)
        so_id = _insert_so()
        sol_id = _insert_line(so_id, item_id, qty_ordered=4, qty_allocated=4)

        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/admin-pick",
            json={"lines": [
                {"so_line_id": sol_id, "bin_id": 3, "quantity": 2},
                {"so_line_id": sol_id, "bin_id": 4, "quantity": 2},
            ]},
            headers=_auth(client),
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["reservations_released"] == 4

        total_on_hand = _inv(item_id, 3)["on_hand"] + _inv(item_id, 4)["on_hand"]
        total_alloc = _inv(item_id, 3)["allocated"] + _inv(item_id, 4)["allocated"]
        assert total_on_hand == 6     # 10 - 4
        assert total_alloc == 0       # 4 - 4
        assert total_on_hand - total_alloc == 6  # available unchanged

    def test_partial_fulfill_then_pick_keeps_available(self, client):
        item_id = _insert_item()
        _set_inv(item_id, 3, qty_on_hand=10, qty_allocated=4)
        so_id = _insert_so()
        sol_id = _insert_line(so_id, item_id, qty_ordered=4, qty_allocated=4)

        # Ship half: short 2, the remaining 2 stay on the SO.
        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 2}]},
            headers=_auth(client),
        )
        assert resp.status_code in (200, 201), resp.get_json()
        line = _line(sol_id)
        assert line["ordered"] == 2
        before = _inv(item_id, 3)

        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/admin-pick",
            json={"lines": [{"so_line_id": sol_id, "bin_id": 3,
                             "quantity": 2}]},
            headers=_auth(client),
        )
        assert resp.status_code == 200, resp.get_json()
        after = _inv(item_id, 3)
        # Half shipped, half picked: reserved units leave with the goods, so
        # available is unchanged and never negative.
        assert after["available"] == before["available"]
        assert after["on_hand"] == before["on_hand"] - 2
        assert after["allocated"] == before["allocated"] - 2


class TestShipKeepsAvailableAndCancelRaisesIt:
    def test_ship_moves_on_hand_and_allocated_together(self, client):
        item_id = _insert_item()
        _set_inv(item_id, 3, qty_on_hand=10, qty_allocated=3)
        so_id = _insert_so()
        sol_id = _insert_line(so_id, item_id, qty_ordered=3, qty_allocated=3)
        before = _inv(item_id, 3)

        assert client.post(
            f"/api/admin/sales-orders/{so_id}/admin-pick",
            json={"lines": [{"so_line_id": sol_id, "bin_id": 3,
                             "quantity": 3}]},
            headers=_auth(client),
        ).status_code == 200
        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/admin-ship",
            json={}, headers=_auth(client))
        assert resp.status_code == 200, resp.get_json()

        after = _inv(item_id, 3)
        assert after["on_hand"] == before["on_hand"] - 3
        assert after["allocated"] == 0
        assert after["available"] == before["available"]  # unchanged on ship

    def test_cancel_pre_pick_releases_reservation(self, client):
        item_id = _insert_item()
        _set_inv(item_id, 3, qty_on_hand=10, qty_allocated=4)
        so_id = _insert_so()
        _insert_line(so_id, item_id, qty_ordered=4, qty_allocated=4)
        before = _inv(item_id, 3)

        resp = client.post(f"/api/admin/sales-orders/{so_id}/cancel",
                           json={}, headers=_auth(client))
        assert resp.status_code == 200, resp.get_json()

        after = _inv(item_id, 3)
        assert after["allocated"] == 0
        assert after["on_hand"] == before["on_hand"]      # goods stay
        assert after["available"] == before["available"] + 4  # rises on cancel


class TestShipmentAdjustmentAudit:
    def _adjustments(self, item_id):
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT quantity_change, reason_code, reason_detail, status, "
            "       adjusted_by FROM inventory_adjustments "
            " WHERE item_id = %s AND reason_code = 'SHIPMENT' "
            " ORDER BY adjustment_id",
            (item_id,),
        )
        rows = cur.fetchall()
        cur.close()
        return rows

    def test_ship_writes_one_negative_shipment_adjustment(self, client):
        item_id = _insert_item()
        _set_inv(item_id, 3, qty_on_hand=10, qty_allocated=3)
        so_id = _insert_so()
        sol_id = _insert_line(so_id, item_id, qty_ordered=3, qty_allocated=3)

        pick = client.post(
            f"/api/admin/sales-orders/{so_id}/admin-pick",
            json={"lines": [{"so_line_id": sol_id, "bin_id": 3,
                             "quantity": 3}]},
            headers=_auth(client))
        assert pick.status_code == 200, pick.get_json()
        adj_ids = pick.get_json()["shipment_adjustment_ids"]
        assert len(adj_ids) == 1

        ship = client.post(f"/api/admin/sales-orders/{so_id}/admin-ship",
                           json={}, headers=_auth(client))
        assert ship.status_code == 200, ship.get_json()

        rows = self._adjustments(item_id)
        assert len(rows) == 1
        change, reason, detail, status, who = rows[0]
        assert change == -3
        assert reason == "SHIPMENT"
        assert "shipment" in detail and "SO " in detail
        assert status == "APPROVED"
        assert who

        # A second ship attempt is refused and must not add a row.
        again = client.post(f"/api/admin/sales-orders/{so_id}/admin-ship",
                            json={}, headers=_auth(client))
        assert again.status_code in (409, 422)
        assert len(self._adjustments(item_id)) == 1

    def test_shipment_adjustment_is_idempotent_per_pick_task(self, client):
        import psycopg2.extras
        from sqlalchemy import text
        from services.sales_order_service import _write_shipment_adjustment
        from db_test_context import get_raw_connection

        class _Adapter:
            """Minimal session shim: the service expects .execute(stmt, params)."""

            def __init__(self, conn):
                self.conn = conn

            def execute(self, stmt, params):
                from sqlalchemy.dialects import postgresql
                compiled = stmt.compile(dialect=postgresql.dialect())
                cur = self.conn.cursor(
                    cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute(str(compiled), params)
                return cur

        item_id = _insert_item()
        _set_inv(item_id, 3, qty_on_hand=5, qty_allocated=0)
        so_id = _insert_so()
        db = _Adapter(get_raw_connection())
        kwargs = dict(so_id=so_id, so_number="SO-DUP", item_id=item_id,
                      bin_id=3, warehouse_id=1, quantity=2,
                      pick_task_id=999999, order_no="#1", username="admin")
        first = _write_shipment_adjustment(db, **kwargs)
        second = _write_shipment_adjustment(db, **kwargs)
        assert first is not None and second is None
        rows = self._adjustments(item_id)
        assert len(rows) == 1
