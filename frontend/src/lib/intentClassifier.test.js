import { describe, it, expect } from 'vitest';
import { classifyIntent, formatHint, INTENTS } from './intentClassifier';

describe('classifyIntent', () => {
  describe('COMPARE', () => {
    it('matches "compare"', () => {
      expect(classifyIntent('compare top products between April and June')).toBe(INTENTS.COMPARE);
    });
    it('matches "comparison"', () => {
      expect(classifyIntent('product comparison Q1 vs Q2')).toBe(INTENTS.COMPARE);
    });
    it('matches "versus"', () => {
      expect(classifyIntent('revenue versus cost by month')).toBe(INTENTS.COMPARE);
    });
    it('matches "对比"', () => {
      expect(classifyIntent('对比四月和六月的销售数据')).toBe(INTENTS.COMPARE);
    });
    it('matches "区别"', () => {
      expect(classifyIntent('这两个产品有什么区别')).toBe(INTENTS.COMPARE);
    });
  });

  describe('RANK', () => {
    it('matches "top 10"', () => {
      expect(classifyIntent('top 10 customers by revenue')).toBe(INTENTS.RANK);
    });
    it('matches "ranking"', () => {
      expect(classifyIntent('ranking of sales representatives')).toBe(INTENTS.RANK);
    });
    it('matches "排名"', () => {
      expect(classifyIntent('销售排名前十的产品')).toBe(INTENTS.RANK);
    });
  });

  describe('TREND', () => {
    it('matches "trend"', () => {
      expect(classifyIntent('sales trend over time')).toBe(INTENTS.TREND);
    });
    it('matches "MoM"', () => {
      expect(classifyIntent('revenue MoM change')).toBe(INTENTS.TREND);
    });
    it('matches "环比"', () => {
      expect(classifyIntent('销售额环比变化')).toBe(INTENTS.TREND);
    });
    it('matches "走势"', () => {
      expect(classifyIntent('产品销售走势分析')).toBe(INTENTS.TREND);
    });
  });

  describe('EXPLAIN', () => {
    it('matches "what is"', () => {
      expect(classifyIntent('what is the average response time')).toBe(INTENTS.EXPLAIN);
    });
    it('matches "how does"', () => {
      expect(classifyIntent('how does the inventory system work')).toBe(INTENTS.EXPLAIN);
    });
    it('matches "为什么"', () => {
      expect(classifyIntent('为什么上个月的订单减少了')).toBe(INTENTS.EXPLAIN);
    });
  });

  describe('GENERATE_ARTIFACT', () => {
    it('matches "create a PPT"', () => {
      expect(classifyIntent('create a PPT for Q2 results')).toBe(INTENTS.GENERATE_ARTIFACT);
    });
    it('matches "make a DOCX"', () => {
      expect(classifyIntent('make a DOCX report')).toBe(INTENTS.GENERATE_ARTIFACT);
    });
    it('matches "generate an Excel"', () => {
      expect(classifyIntent('generate an Excel spreadsheet of this data')).toBe(INTENTS.GENERATE_ARTIFACT);
    });
    it('matches "build a dashboard"', () => {
      expect(classifyIntent('build a dashboard of our KPIs')).toBe(INTENTS.GENERATE_ARTIFACT);
    });
    it('matches "制作PPT"', () => {
      expect(classifyIntent('帮我制作一个PPT')).toBe(INTENTS.GENERATE_ARTIFACT);
    });
    it('matches "生成报告文档"', () => {
      expect(classifyIntent('帮我生成一个报告文档')).toBe(INTENTS.GENERATE_ARTIFACT);
    });
    it('matches "创建Word文档"', () => {
      expect(classifyIntent('创建Word文档总结销售数据')).toBe(INTENTS.GENERATE_ARTIFACT);
    });
  });

  describe('GENERAL fallback', () => {
    it('returns general for ambiguous text', () => {
      expect(classifyIntent('你好')).toBe(INTENTS.GENERAL);
    });
    it('returns general for empty string', () => {
      expect(classifyIntent('')).toBe(INTENTS.GENERAL);
    });
    it('returns general for null/undefined', () => {
      expect(classifyIntent(null)).toBe(INTENTS.GENERAL);
      expect(classifyIntent(undefined)).toBe(INTENTS.GENERAL);
    });
  });
});

describe('formatHint', () => {
  it('returns compare hint', () => {
    const hint = formatHint(INTENTS.COMPARE);
    expect(hint).toContain('markdown table');
    expect(hint).toContain('Key Insights');
  });

  it('returns rank hint', () => {
    const hint = formatHint(INTENTS.RANK);
    expect(hint).toContain('numbered markdown table');
    expect(hint).toContain('Top Takeaway');
  });

  it('returns trend hint', () => {
    const hint = formatHint(INTENTS.TREND);
    expect(hint).toContain('MoM/YoY');
  });

  it('returns explain hint', () => {
    const hint = formatHint(INTENTS.EXPLAIN);
    expect(hint).toContain('TL;DR');
    expect(hint).toContain('Quick Summary');
  });

  it('returns artifact hint', () => {
    const hint = formatHint(INTENTS.GENERATE_ARTIFACT);
    expect(hint).toContain('FILE-FORMAT INTENT');
    expect(hint).toContain('run_sandbox_skill');
  });

  it('returns empty for general', () => {
    expect(formatHint(INTENTS.GENERAL)).toBe('');
  });
});
