"""Eval question set for the semantic catalog (Phase 1).

Ground-truth expected tables are drawn from the warehouse discovery report
(`docs/superpowers/plans/2026-08-07-warehouse-discovery-report.md`).
Used by test_eval_table_recall.py — run manually, not in the CI suite.
"""

EVAL_QUESTIONS: list[dict] = [
    {
        "question": "最近乙烯华东市场价格走势如何？",
        "expected_tables": ["md_t_lz_price", "sale_erp_v_ethylene_price"],
    },
    {
        "question": "上个月产品库存变化趋势是怎样的？",
        "expected_tables": ["erp_t_stk_inventory", "erp_v_stk_inventory"],
    },
    {
        "question": "本月采购入库了多少吨原材料？",
        "expected_tables": ["erp_t_stk_instock", "erp_t_stk_instockentry"],
    },
    {
        "question": "近一年工厂开工率或产量变化情况？",
        "expected_tables": ["erp_t_sp_instock", "erp_t_sp_instockentry"],
    },
    {
        "question": "惠州工厂最近的销售量和成交价？",
        "expected_tables": ["erp_v_sale_huizhou"],
    },
    {
        "question": "广东地区的销售情况如何？",
        "expected_tables": ["erp_v_sale_guangdong"],
    },
    {
        "question": "哪些合同还没有执行完成？",
        "expected_tables": ["erp_v_contract", "erp_v_contract_execution", "erp_t_crm_contractentry"],
    },
    {
        "question": "每月原材料接收量有多少？",
        "expected_tables": ["erp_v_raw_material_receiving"],
    },
    {
        "question": "我们的主要供应商有哪些？",
        "expected_tables": ["erp_t_bd_supplier"],
    },
    {
        "question": "去年的销售明细和运费成本？",
        "expected_tables": ["erp_product_sales_details"],
    },
    {
        "question": "最近有哪些预测决策点被记录？",
        "expected_tables": ["forecast_decision_points", "forecast_decision_snapshots"],
    },
    {
        "question": "预测准确率的历史记录怎么样？",
        "expected_tables": ["forecast_accuracy_log"],
    },
    {
        "question": "最近有没有重要的市场事件？",
        "expected_tables": ["intelligence_events"],
    },
]
