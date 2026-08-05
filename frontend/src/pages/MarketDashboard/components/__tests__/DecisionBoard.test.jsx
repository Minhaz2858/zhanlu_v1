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
        id: 'dcpd', name_zh: '双环戊二烯', name_en: 'DCPD', code: 'dcpd',
        current_price: 9800, price_date: '2026-08-03', forecast_base: null,
        expected_change_pct: null, p_rise: null, action: 'watch',
        action_label_zh: '建议观望', confidence: 'low',
        trust_tier: 'low', trust_label_zh: '低可信', trust_color: 'red',
        directional_accuracy: null, intel_event_count: 0, intel_bias: 'neutral',
        volatility_regime: null,
        one_liner_zh: '数据不足,建议观望',
        narrative_zh: '双环戊二烯当前预测数据不足,建议保持观望。',
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

describe('DecisionBoard', () => {
  it('renders briefing and product rows with name + code', async () => {
    renderBoard();
    expect(await screen.findByText(/重点关注/)).toBeInTheDocument();
    expect(await screen.findByText('异戊二烯')).toBeInTheDocument();
    expect(screen.getByText('isoprene')).toBeInTheDocument();
    expect(screen.getByText('双环戊二烯')).toBeInTheDocument();
    expect(screen.getByText('建议备货')).toBeInTheDocument();
    expect(screen.getByText('建议观望')).toBeInTheDocument();
    expect(screen.getByText('68%')).toBeInTheDocument();
  });

  it('row click fires onSelectProduct', async () => {
    const onSelect = vi.fn();
    renderBoard({ onSelectProduct: onSelect });
    const row = await screen.findByText('异戊二烯');
    fireEvent.click(row.closest('tr'));
    expect(onSelect).toHaveBeenCalledWith('isoprene');
  });

  it('chevron expands the card showing full narrative', async () => {
    renderBoard();
    const btn = await screen.findByLabelText('展开 异戊二烯');
    fireEvent.click(btn);
    expect(
      await screen.findByText(/异戊二烯现价 12,100 元\/吨/),
    ).toBeInTheDocument();
    expect(screen.getByText(/方向准确率/)).toBeInTheDocument();
    // collapse again
    fireEvent.click(btn);
    expect(screen.queryByText(/异戊二烯现价 12,100 元\/吨/)).not.toBeInTheDocument();
  });

  it('renders structured analyst brief sections when present', async () => {
    const { marketDashboardApi } = await import('@/api/marketDashboard');
    const withBrief = {
      ...FIXTURE,
      products: [
        {
          ...FIXTURE.products[0],
          analyst_brief: {
            summary_zh: '异戊二烯现价 12,100 元/吨,预计涨至 12,450 元/吨(+2.9%)。',
            why_price_zh: '上游裂解碳五近30日+1.8%,按 0.68 弹性传导约 +1.2%。',
            why_action_zh: '建议提前备货 —— 上涨概率≥70% 且方向准确率达标。',
            watch_triggers_zh: ['方向准确率回升至 55% 以上时重新评估'],
            risk_zh: '近期无重大市场事件,价格主要由供需基本面驱动。',
            source: 'llm', day: 7, generated_at: '2026-08-04T00:00:00+00:00',
          },
        },
        FIXTURE.products[1],
      ],
    };
    marketDashboardApi.decisionBoard.mockResolvedValueOnce(withBrief);
    renderBoard();
    const btn = await screen.findByLabelText('展开 异戊二烯');
    fireEvent.click(btn);
    expect(await screen.findByText(/上游裂解碳五近30日/)).toBeInTheDocument();
    expect(screen.getByText('关注触发')).toBeInTheDocument();
    expect(screen.getByText(/方向准确率回升至 55%/)).toBeInTheDocument();
    expect(screen.getByText('AI 分析师撰写')).toBeInTheDocument();
  });

  it('shows no-events note when intel_event_count is 0', async () => {
    renderBoard();
    const btn = await screen.findByLabelText('展开 双环戊二烯');
    fireEvent.click(btn);
    expect(await screen.findByText('近期无重大市场事件')).toBeInTheDocument();
  });
});
