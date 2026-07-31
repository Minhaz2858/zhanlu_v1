import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import SnapshotGrid from '../SnapshotGrid';

const items = [
  { id: 'naphtha', name: 'Japanese Naphtha', price: 612.5, date: '2026-07-30',
    change_pct: 1.2, group: 'upstream', unit: 'CNY/mt' },
  { id: 'crude_oil', name: 'Crude Oil', price: null, date: null,
    change_pct: null, group: 'upstream', note: 'table mapping not configured in v1' },
];

describe('SnapshotGrid', () => {
  it('renders a card per item', () => {
    render(<SnapshotGrid items={items} />);
    expect(screen.getByText('Japanese Naphtha')).toBeInTheDocument();
    expect(screen.getByText('Crude Oil')).toBeInTheDocument();
  });

  it('shows price for mapped products', () => {
    render(<SnapshotGrid items={items} />);
    expect(screen.getByText('612.5')).toBeInTheDocument();
  });

  it('shows NO_DATA_LABEL when price is null', () => {
    render(<SnapshotGrid items={items} />);
    expect(screen.getAllByText(/No Data Available/i).length).toBeGreaterThan(0);
  });
});
