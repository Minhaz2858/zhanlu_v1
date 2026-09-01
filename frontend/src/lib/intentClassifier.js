/**
 * Intent classifier — deterministic, no LLM call.
 * Runs BEFORE the LLM to inject a one-line hint into the system prompt
 * so the model knows how to structure its answer.
 *
 * Classifications:
 *   compare | rank | trend | explain | generate_artifact | general
 */
export const INTENTS = {
  COMPARE: 'compare',
  RANK: 'rank',
  TREND: 'trend',
  EXPLAIN: 'explain',
  GENERATE_ARTIFACT: 'generate_artifact',
  GENERAL: 'general',
};

const PATTERNS = [
  // Order matters — more specific patterns first.
  {
    intent: INTENTS.GENERATE_ARTIFACT,
    // Two-part check: (1) creation verb + (2) artifact target
    regex: /(?:(?:\b(?:make|create|generate|build)\b)|(?:为(?:我)?)?(?:制作|生成|创建|构建|写)|帮我(?:写|做|弄|搞|画)).*(?:PPT|DOCX?|XLSX?|PDF|HTML|演示文稿|Word|Excel|PowerPoint|文档|报告文档|公文|表格|仪表盘|dashboard|report|presentation|spreadsheet|word\s*document|excel\s*workbook|powerpoint|slide\s*deck|pitch\s*deck|网页|html\s*页面|文件|file)/i,
  },
  {
    intent: INTENTS.COMPARE,
    // \b doesn't work with Chinese — use an alternation of standalone terms
    regex: /\b(?:compare|comparison|versus|vs\.?|diff(?:erence)?\s*(?:between|of)?)\b|(?:对比|比较|相比较|相[比较]|区别|差异)/i,
  },
  {
    intent: INTENTS.TREND,
    regex: /\b(?:trend|trends|over\s*time|(?:month|quarter|year)[- ]?(?:over|on)[- ]?(?:month|quarter|year)|MoM|YoY|QoQ|seasonal)\b|(?:走势|趋势|时间序列|逐月|环比|同比|随时间|变化趋势)/i,
  },
  {
    intent: INTENTS.RANK,
    regex: /\b(?:top\s*\d+|rank(?:ing)?|leaderboard)\b|(?:排行|排名|前\d+|最(?:好|高|大|多|快|优))/i,
  },
  {
    intent: INTENTS.EXPLAIN,
    regex: /\b(?:what\s*(?:is|are|does)|how\s*(?:does|to|do|can|should|would|much\s*longer)|why\s*(?:is|are|does|would|should)|explain|elaborate|describe)\b|(?:定义|什么是|如何|怎么|为什么|解释|说明|请[问说]明)/i,
  },
];

/**
 * Classify a user message into one of the intent categories.
 * @param {string} text - The user's message text.
 * @returns {string} One of the INTENTS values.
 */
export function classifyIntent(text) {
  if (!text || typeof text !== 'string') return INTENTS.GENERAL;
  const normalized = text.trim();

  for (const { intent, regex } of PATTERNS) {
    if (regex.test(normalized)) {
      return intent;
    }
  }

  return INTENTS.GENERAL;
}

/**
 * Return a one-line hint to inject into the system prompt based on the
 * classified intent. This primes the LLM to structure its answer correctly.
 * @param {string} intent - One of the INTENTS values.
 * @returns {string} A concise directive for the LLM.
 */
export function formatHint(intent) {
  switch (intent) {
    case INTENTS.COMPARE:
      return (
        'STRUCTURED RESPONSE RULE: Always present comparisons as a markdown table '
        + 'with Period | Metric | Value columns. Use sortable headers and highlight '
        + 'the winner. End with a "Key Insights" bullet list of the 3 most important findings.'
      );
    case INTENTS.RANK:
      return (
        'STRUCTURED RESPONSE RULE: Always present rankings as a numbered markdown table '
        + '(Rank | Name | Value columns). Include the total count and the range. '
        + 'End with a "Top Takeaway" bullet list.'
      );
    case INTENTS.TREND:
      return (
        'STRUCTURED RESPONSE RULE: Show trends as a markdown table with '
        + 'Period | Value | MoM/YoY Change columns. Include % change. '
        + 'End with directional analysis and 1-2 projection bullets.'
      );
    case INTENTS.EXPLAIN:
      return (
        'STRUCTURED RESPONSE RULE: Start with a one-sentence TL;DR. '
        + 'Then break down the answer with headings, short paragraphs, and examples. '
        + 'End with a "Quick Summary" of 2-3 takeaway bullets.'
      );
    case INTENTS.GENERATE_ARTIFACT:
      return (
        'FILE-FORMAT INTENT (HARD RULE): The user wants a downloadable file. '
        + 'Use ask_data_agent to fetch data, then run_sandbox_skill to produce the file. '
        + 'Do NOT stop at an in-chat card — the user asked for a FILE.'
      );
    default:
      return '';
  }
}

export default { classifyIntent, formatHint, INTENTS };
