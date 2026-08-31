---
name: stock-research-report
description: Use for Chinese equity research, industry tracking, investment notes, and financial research documents in Guotai Haitong / Haitong International style. Triggers on keywords: Guotai Haitong, Haitong International, industry tracking, stock research, securities research, 行业跟踪, 个股研究, 证券研究, 研报, 股票研究. Supports domestic and international dual templates.
---

# Stock Research Report

Generate securities research reports in Guotai Haitong / Haitong International style — Chinese equity research, industry tracking, and investment notes.

## When to use

- "Guotai Haitong", "Haitong International" style research
- "行业跟踪" (industry tracking), "个股研究" (stock research), "证券研究" (securities research)
- Chinese equity research reports, investment notes, financial research documents
- Dual-template requests: domestic (A股) and international (港股/美股) formats

## Report structure (domestic template)

1. **报告封面** — 证券名称, 代码, 评级 (买入/增持/中性/减持), 目标价, 报告日期, 分析师
2. **投资要点** — 核心逻辑 3-5 条 (评级理由, 催化剂, 风险提示)
3. **盈利预测与估值** — 财务预测表 (营收/净利润/毛利率/EPS/PE), 估值方法 (PE/PB/DCF), 目标价推导
4. **公司概况** — 主营业务, 商业模式, 股权结构, 产能/渠道/客户
5. **行业分析** — 行业空间, 增速, 竞争格局, 产业链位置, 政策环境
6. **公司分析** — 分业务拆解, 量价分析, 成本与费用, 核心竞争优势
7. **财务分析** — 三大报表历史与预测, 关键财务比率
8. **风险提示** — 经营风险, 行业风险, 政策风险, 估值风险

## Report structure (international template)

1. Cover — company, ticker, rating (Buy/Accumulate/Neutral/Reduce/Sell), target price, date, analyst
2. Investment thesis — key drivers, catalysts, risks
3. Earnings forecast & valuation — forecast tables, valuation methodology
4. Company overview — business model, segments, management
5. Industry analysis — TAM, growth, competition, regulatory
6. Financials — statements + ratios
7. Risk factors
8. Disclosures

## Writing rules

- 评级必须有明确的盈利预测和估值支撑; 目标价必须有推导过程
- 每个结论对应数据; 财务数字必须来自真实数据源或明确标注预测
- 预测表保持会计口径一致 (营收/归母净利润/毛利率/净利率/EPS)
- 表格为主、文字精炼 — 卖方研报风格
- 中英文均可; 国内模板用中文, 国际模板可用英文
- 数据不得虚构 — 无数据时明确标注 "待验证/illustrative"

## Output

DOCX or PDF (report), PPTX (投研纪要/路演版). Match the requested template (domestic/international).
