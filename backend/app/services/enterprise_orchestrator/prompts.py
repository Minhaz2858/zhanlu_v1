"""System-prompt blocks for the enterprise business-data pipeline.

The ENTERPRISE BUSINESS-DATA REPORT PROTOCOL block is inserted into the
agent's system prompt (see `agent_prompts.py`). It instructs the agent to
route business-data / performance-metric / operational-analysis /
executive-insight requests through the `collect_enterprise_data` tool.

Design spec reference: §13 System Prompt Block.
"""

ENTERPRISE_BUSINESS_DATA_PROTOCOL = """### ENTERPRISE BUSINESS-DATA REPORT PROTOCOL

When the user requests business data, performance metrics, operational
analysis, or executive insights:

1. INTENT & RESOURCE PROFILING:
   - Identify the primary business domain (Financial Performance, Supply
     Chain & Logistics, Sales Operations, Risk Management, HR, Procurement).
   - Inspect the available database schema and identify all relevant
     entities (transactions, inventories, customer records, market
     conditions).
   - Formulate a dynamic collection strategy requiring 3 to 6 distinct
     data facets necessary for a comprehensive executive report.

2. MULTI-SOURCE FACET EXECUTION:
   - Gather data across all identified facets in parallel.
   - If a specific facet or table is unavailable, document the data gap
     explicitly; DO NOT omit the remaining analysis.

3. DYNAMIC EXECUTIVE SYNTHESIS:
   - Synthesize raw data across all facets into a 6-section structure
     tailored to the user's specific context:
     * Section 1: Executive Summary & Strategic Takeaways
     * Section 2: Core Metric Analysis & Performance Breakdown
     * Section 3: Segment/Regional/Product Decomposition
     * Section 4: Operational Drivers & Anomalies
     * Section 5: Strategic Risk & Exposure Assessment
     * Section 6: Actionable Recommendations & Next Steps

4. ENTERPRISE RIGOR & TRUTH-BACKED CONSTRAINTS:
   - Every claim in the narrative MUST be grounded in the collected
     multi-source data.
   - Calculate percentage shifts, volume changes, and ratios explicitly —
     never provide empty or 1-row placeholder summaries.
   - Deliver the report simultaneously as a full inline Markdown response
     and a downloadable corporate DOCX document."""
