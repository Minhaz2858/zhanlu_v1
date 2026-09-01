"""Pydantic response schemas for the report endpoints.

Field names mirror the platform's frontend TypeScript interfaces:
- CustomerSalesReport.tsx:29-51 (SalesReport)
- ContractPerformanceReport.tsx:18-60 (ContractReport)
- AiAnalysisResult (SSE done event payload)
"""
from typing import Optional
from pydantic import BaseModel, Field


# ── Customer Sales Report ──────────────────────────────────────────────

class CustomerSalesSummary(BaseModel):
    total_sales_qty: float = 0
    total_sales_amount_wan: float = 0
    sales_qty_yoy: float = 0
    sales_amount_yoy: float = 0


class CustomerShareItem(BaseModel):
    name: str
    value: float


class ProductGrowthItem(BaseModel):
    product: str
    qty: float
    unit: str = "吨"
    yoy: float = 0


class WeekPricePoint(BaseModel):
    week: str
    # dynamic product-name keys added via model_config extra="allow"
    model_config = {"extra": "allow"}


class SalesDetailWeek(BaseModel):
    week: str
    qty: float
    price: float


class SalesDetailRow(BaseModel):
    product: str
    customer: str
    weeks: list[SalesDetailWeek] = []


class CustomerSalesDebug(BaseModel):
    price_source_counts: dict[str, int] = {}
    amount_source_counts: dict[str, int] = {}
    sample_rows: list[dict] = []
    sample_limit: int = 200


class CustomerSalesReportResponse(BaseModel):
    month: str
    available_months: list[str] = []
    available_materials: list[str] = []
    org_name: str = ""
    summary: CustomerSalesSummary
    customer_share: list[CustomerShareItem] = []
    product_growth: list[ProductGrowthItem] = []
    weeks: list[str] = []
    price_chart: list[dict] = []   # list of {week, productA, productB, ...}
    sales_chart: list[dict] = []   # list of {week, productA, productB, ...}
    sales_detail: list[SalesDetailRow] = []
    products: list[str] = []
    debug: Optional[CustomerSalesDebug] = None


# ── Contract Performance Report ────────────────────────────────────────

class ContractPerformanceSummary(BaseModel):
    contract_amount_yi: float = 0        # 亿元
    unfulfilled_orders: int = 0
    completion_rate: float = 0
    contract_qty: float = 0
    out_qty: float = 0
    not_out_qty: float = 0
    invoice_amount_wan: float = 0        # 万元
    uninvoiced_amount_wan: float = 0     # 万元
    # YoY
    amount_yoy: float = 0
    qty_yoy: float = 0
    unfulfilled_yoy: float = 0
    completion_rate_yoy: float = 0
    contract_qty_yoy: float = 0
    out_qty_yoy: float = 0
    not_out_qty_yoy: float = 0
    invoice_yoy: float = 0
    uninvoiced_yoy: float = 0


class UnshippedContractItem(BaseModel):
    product: str
    product_type: str = ""
    bill_no: str
    customer: str
    delivery_date: str = ""
    contract_qty: float
    unshipped_qty: float


class ExecutionChartItem(BaseModel):
    product: str
    contract_qty: float
    out_qty: float
    execution_rate: float


class ContractPerformanceResponse(BaseModel):
    start_date: str
    end_date: str
    available_materials: list[str] = []
    org_name: str = ""
    summary: ContractPerformanceSummary
    unshipped_contracts: list[UnshippedContractItem] = []
    execution_chart: list[ExecutionChartItem] = []
    ai_analysis: Optional["AiAnalysisResult"] = None


# ── AI Analysis (SSE done event) ───────────────────────────────────────

class AiAnalysisResult(BaseModel):
    chart_top: str = ""
    chart_bottom: str = ""
    summary_paragraph: str = ""
    summary_bullets: list[str] = []
    unshipped_analysis: str = ""
