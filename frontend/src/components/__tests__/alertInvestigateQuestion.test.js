import { describe, it, expect } from 'vitest';
import { buildInvestigateQuestion } from '../Sidebar';

describe('buildInvestigateQuestion', () => {
  it('price_spike — positive change with product name and price', () => {
    const alert = {
      alert_type: 'price_spike',
      product_name: 'Blowing Agent',
      change_7d_pct: 14.0,
      price: 6316.11,
    };
    const q = buildInvestigateQuestion(alert);
    expect(q).toContain('Blowing Agent');
    expect(q).toContain('+14.0%');
    expect(q).toContain('6,316.11 CNY/ton');
    expect(q).toContain('price increase window');
    expect(q).toContain('inventory level');
    expect(q).toContain('shipment pace');
  });

  it('price_drop — negative change', () => {
    const alert = {
      alert_type: 'price_drop',
      product_name: 'HDPE',
      change_7d_pct: -5.3,
      price: 8200,
    };
    const q = buildInvestigateQuestion(alert);
    expect(q).toContain('HDPE');
    expect(q).toContain('5.3%');
    expect(q).toContain('8,200 CNY/ton');
    expect(q).toContain('supplier signaling weakness');
    expect(q).toContain('procurement pivot');
  });

  it('regional_arbitrage — with source/destination', () => {
    const alert = {
      alert_type: 'regional_arbitrage',
      product_name: 'LLDPE',
      change_7d_pct: 3.2,
      price: 9100,
      source: 'East China',
      destination: 'South China',
    };
    const q = buildInvestigateQuestion(alert);
    expect(q).toContain('Regional arbitrage');
    expect(q).toContain('LLDPE');
    expect(q).toContain('East China');
    expect(q).toContain('South China');
    expect(q).toContain('higher-margin region');
  });

  it('capacity_change — with message', () => {
    const alert = {
      alert_type: 'capacity_change',
      product_name: 'PVC',
      change_7d_pct: -8,
      message: 'Sudden shutdown at Plant X',
    };
    const q = buildInvestigateQuestion(alert);
    expect(q).toContain('Capacity signal');
    expect(q).toContain('PVC');
    expect(q).toContain('Sudden shutdown at Plant X');
    expect(q).toContain('supply tightness');
  });

  it('fallback — unknown type with message, zero change', () => {
    const alert = {
      alert_type: 'custom_type',
      product_name: 'PP',
      change_7d_pct: 0,
      message: 'Unusual pattern detected',
    };
    const q = buildInvestigateQuestion(alert);
    expect(q).toContain('Investigate this alert');
    expect(q).toContain('Unusual pattern detected');
  });

  it('fallback — no type, no message, zero change', () => {
    const alert = {
      product_name: 'PS',
      change_7d_pct: 0,
    };
    const q = buildInvestigateQuestion(alert);
    expect(q).toContain('Analyze PS');
    expect(q).toContain('market signal');
  });

  it('missing product_name falls back to product_id', () => {
    const alert = {
      alert_type: 'price_spike',
      product_id: 'SKU-123',
      change_7d_pct: 7.0,
      price: 5000,
    };
    const q = buildInvestigateQuestion(alert);
    expect(q).toContain('SKU-123');
  });

  it('missing price uses "price TBD"', () => {
    const alert = {
      alert_type: 'price_spike',
      product_name: 'ABS',
      change_7d_pct: 3.0,
    };
    const q = buildInvestigateQuestion(alert);
    expect(q).toContain('price TBD');
  });

  it('missing both product_name and product_id falls back to "this product"', () => {
    const alert = {
      alert_type: 'price_drop',
      change_7d_pct: -2.0,
      price: 1000,
    };
    const q = buildInvestigateQuestion(alert);
    expect(q).toContain('this product');
  });

  it('positive change_7d_pct without explicit alert_type triggers price_spike path', () => {
    const alert = {
      product_name: 'EPS',
      change_7d_pct: 6.0,
      price: 7500,
    };
    const q = buildInvestigateQuestion(alert);
    expect(q).toContain('price increase window');
  });

  it('negative change_7d_pct without explicit alert_type triggers price_drop path', () => {
    const alert = {
      product_name: 'GPPS',
      change_7d_pct: -3.0,
      price: 8800,
    };
    const q = buildInvestigateQuestion(alert);
    expect(q).toContain('supplier signaling weakness');
  });
});
