/**
 * Inventory page Available must be a per-bin number.
 *
 * Terry's screenshot: SKU 810199503806 showed RECV-01 as "On Hand 0,
 * Available -156". The stock data was right; the page subtracted the
 * ITEM-level commitment from the BIN-level on-hand, so every empty bin
 * rendered as -(everything unshipped for that item).
 *
 * Per-bin Available = quantity_on_hand - quantity_allocated.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

const apiGetMock = vi.fn();
vi.mock('../api.js', () => ({
  api: {
    get: (...a) => apiGetMock(...a),
    post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
  },
}));
vi.mock('../auth.jsx', () => ({
  useAuth: () => ({ user: { role: 'ADMIN', allowed_overrides: [] } }),
}));
vi.mock('../warehouse.jsx', () => ({
  useWarehouse: () => ({ warehouseId: 1 }),
}));

import Inventory from '../pages/Inventory.jsx';

const json = (body) => Promise.resolve({
  ok: true, status: 200, json: () => Promise.resolve(body),
});

function row(overrides) {
  return {
    inventory_id: 1, item_id: 3731, sku: '810199503806',
    item_name: 'One Piece OP18', bin_id: 3, bin_code: 'BULK-01',
    zone_id: 4, zone_name: 'Bulk', quantity_on_hand: 228,
    quantity_allocated: 156, quantity_available: 72, committed_to_orders: 156,
    picked_not_shipped: 0, lot_number: null, last_counted_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  apiGetMock.mockReset();
  apiGetMock.mockImplementation((url) => {
    if (url.startsWith('/admin/warehouses')) {
      return json({ warehouses: [{ warehouse_id: 1, warehouse_code: 'WH-01', warehouse_name: 'Main' }] });
    }
    if (url.startsWith('/admin/bins')) {
      return json({ bins: [{ bin_id: 3, bin_code: 'BULK-01', zone_id: 4, zone_name: 'Bulk' }] });
    }
    if (url.startsWith('/admin/inventory')) {
      return json({
        inventory: [
          row(),
          row({
            inventory_id: 2, bin_id: 1, bin_code: 'RECV-01', zone_name: 'Receiving',
            quantity_on_hand: 0, quantity_allocated: 0,
            quantity_available: 0, committed_to_orders: 156,
          }),
        ],
        total: 2, page: 1, per_page: 50, pages: 1,
      });
    }
    return json({});
  });
});

describe('Inventory per-bin availability', () => {
  it('shows the stocked bin as on_hand - allocated', async () => {
    render(<Inventory />);
    await waitFor(() => expect(screen.getAllByText('BULK-01').length).toBeGreaterThan(0));
    expect(screen.getByText('72')).toBeTruthy();
  });

  it('shows an empty bin as 0, never as -(item commitment)', async () => {
    render(<Inventory />);
    await waitFor(() => expect(screen.getAllByText('RECV-01').length).toBeGreaterThan(0));
    expect(screen.queryByText('-156')).toBeNull();
    const recvRow = screen.getAllByText('RECV-01')[0].closest('tr');
    const cells = Array.from(recvRow.querySelectorAll('td')).map((td) => td.textContent.trim());
    expect(cells.filter((t) => /^-\d+$/.test(t))).toEqual([]);
  });

  it('labels the item-level commitment as an item total', async () => {
    render(<Inventory />);
    await waitFor(() => expect(screen.getAllByText('BULK-01').length).toBeGreaterThan(0));
    expect(screen.getByText('Committed (item total)')).toBeTruthy();
  });
});
