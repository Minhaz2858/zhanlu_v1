// P0.4: Honesty-gated product row renders "—" for p_rise/expected-move
// and shows "Directional Reference" / "方向性参考" badge, NOT "Insufficient Data".
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const { FIXTURE } = vi.hoisted(() => ({
  FIXTURE: {
    group: 'midstream', day: 7, generated_at: '2026-08-04T00:00:00+00:00',
    briefing_zh: '未来 7 天重点关注:异戊二烯看涨(上涨概率 68%),建议提前备货。',
    products: [
      {
        id: 'isoprene', name_zh: '异戊二烯', name_en: 'Isoprene', code: 'isoprene',
        current_price: 12100, price_date: '2026-08-03', forecast_base: 12450,
        expected_change_pct: 0.029, p_rise: 0.68, action: 'buy',
        action_label_zh: '建议备货', confidence: 'medium',
        trust_tier: 'high', trust_label_zh: '高可信', trust_color: 'green',
        directional_accuracy: 0.72, intel_event_count: 3, intel_bias: 'up',
        volatility_regime: 'NORMAL',
        one_liner_zh: '预计+2.9%,上涨概率 68%,建议备货',
        narrative_zh: '异戊二烯现价 12,100 元/吨,预计未来 7 天涨至 12,450 元/吨。',
        skus: [],
      },
      {
        // P0.4: honesty-gate downgrade product
        id: 'blowing_agent', name_zh: '发泡剂', name_en: 'Blowing Agent', code: 'blowing_agent',
        current_price: 8500, price_date: '2026-08-03', forecast_base: 8500,
        expected_change_pct: 0.0, p_rise: null,  // P0.3 returns null when gated
        action: 'watch', action_label_zh: '建议观望', confidence: 'low',
        trust_tier: 'directional', trust_label_zh: '方向性参考', trust_color: 'amber',
        directional_accuracy: null, intel_event_count: 0, intel_bias: 'neutral',
        volatility_regime: 'NORMAL',
        one_liner_zh: '模型暂无统计优势,展示保守基线,建议观望',
        narrative_zh: '发泡剂当前模型暂无统计优势,展示保守基线预测,建议保持观望。',
        skus: [],
      },
    ],
  },
}));

vi.mock('@/api/marketDashboard', () => ({
  marketDashboardApi: {
    decisionBoard: vi.fn().mockResolvedValue(FIXTURE),
    forecastChart: vi.fn().mockResolvedValue({ product_id: 'isoprene', points: [] }),
  },
}));

import DecisionBoard from '../DecisionBoard';

function renderBoard(props = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <DecisionBoard group="midstream" selectedId={null} onSelectProduct={() => {}} {...props} />
    </QueryClientProvider>,
  );
}

describe('P0.4: Honesty-gate downgrade semantics', () => {
  it('gated product shows "—" for p_rise instead of "50%"', async () => {
    renderBoard();
    // Find the blowing agent row by its trust label
    const trustBadge = await screen.findByText('方向性参考');
    expect(trustBadge).toBeInTheDocument();
    // The p_rise cell for blowing_agent should show "—" not "50%"
    // All rows are in the table; blowing_agent is the second product
    const rows = screen.getAllByRole('row');
    // Find the row containing "发泡剂"
    const blowingRow = rows.find(r => r.textContent.includes('发泡剂'));
    expect(blowingRow).toBeTruthy();
    // p_rise should be "—" (since p_rise: null)
    // The cell content should not contain "50%"
    expect(blowingRow.textContent).not.toMatch(/50%/);
  });

  it('gated product shows "—" for expected-change instead of "+0.0%"', async () => {
    renderBoard();
    await screen.findByText('方向性参考');
    const rows = screen.getAllByRole('row');
    const blowingRow = rows.find(r => r.textContent.includes('发泡剂'));
    expect(blowingRow).toBeTruthy();
    // expected_change_pct should be suppressed → "—" not "+0.0%"
    expect(blowingRow.textContent).not.toMatch(/\+0\.0%/);
  });

  it('gated product shows "方向性参考" badge, NOT "数据不足"', async () => {
    renderBoard();
    expect(await screen.findByText('方向性参考')).toBeInTheDocument();
    expect(screen.queryByText('数据不足')).not.toBeInTheDocument();
  });

  it('gated product one_liner says "模型暂无统计优势" not "数据不足"', async () => {
    renderBoard();
    const oneLiner = await screen.findByText(/模型暂无统计优势/);
    expect(oneLiner).toBeInTheDocument();
  });
});
