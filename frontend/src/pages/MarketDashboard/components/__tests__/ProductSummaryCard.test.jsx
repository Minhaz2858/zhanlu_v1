import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

import ProductSummaryCard, {
  formatPriceChip,
  formatSalesChip,
} from '../ProductSummaryCard';

describe('ProductSummaryCard', () => {
  it('renders name, price, and both chips', () => {
    render(
      <ProductSummaryCard
        name="Brent Crude Oil"
        price={84.09}
        priceChangePct={15.12}
        sales7dChangePct={-54.18}
        high={6316.11}
        low={5486.73}
        sales7dTons={1411.7}
      />,
    );

    expect(screen.getByText('Brent Crude Oil')).toBeInTheDocument();
    expect(screen.getByText('84.09')).toBeInTheDocument();
    expect(screen.getByText('+15.12%')).toBeInTheDocument();
    expect(screen.getByText('-54.18%')).toBeInTheDocument();
  });

  it('renders the High/Low/7d sales footer', () => {
    render(
      <ProductSummaryCard
        name="Isoprene"
        price={8100}
        priceChangePct={0}
        sales7dChangePct={0}
        high={8310}
        low={8000}
        sales7dTons={950}
      />,
    );

    expect(screen.getByText('High 8,310')).toBeInTheDocument();
    expect(screen.getByText('Low 8,000')).toBeInTheDocument();
    expect(screen.getByText(/7d sales 950t/)).toBeInTheDocument();
  });

  it('degrades gracefully when fields are null', () => {
    render(
      <ProductSummaryCard
        name="Styrene"
        price={null}
        priceChangePct={null}
        sales7dChangePct={null}
        high={null}
        low={null}
        sales7dTons={null}
      />,
    );

    expect(screen.getByText('Styrene')).toBeInTheDocument();
    expect(screen.getAllByText('--').length).toBeGreaterThanOrEqual(1);
  });
});

describe('chip formatting helpers', () => {
  it('price up is red, down is green, zero is neutral', () => {
    expect(formatPriceChip(5).arrow).toBe('▲');
    expect(formatPriceChip(5).classes).toContain('rose');
    expect(formatPriceChip(-5).arrow).toBe('▼');
    expect(formatPriceChip(-5).classes).toContain('emerald');
    expect(formatPriceChip(0).arrow).toBe('');
    expect(formatPriceChip(0).label).toBe('0.00%');
  });

  it('sales inverts: up is blue, down is red', () => {
    expect(formatSalesChip(5).arrow).toBe('▲');
    expect(formatSalesChip(5).classes).toContain('blue');
    expect(formatSalesChip(-5).arrow).toBe('▼');
    expect(formatSalesChip(-5).classes).toContain('rose');
  });

  it('null change renders neutral -- chip', () => {
    expect(formatPriceChip(null).label).toBe('--');
    expect(formatPriceChip(null).arrow).toBe('');
    expect(formatSalesChip(undefined).label).toBe('--');
  });
});
