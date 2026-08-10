# Tier 3 Procurement Data Sourcing Questionnaire

> **Purpose:** Unblock the three Tier 3 fundamental-data feeds (operating rate, port inventory, import prices) that are currently BLOCKED. These feeds provide the highest long-term accuracy ceiling — no model can replace them.
>
> **Action:** Send the email below to the procurement / market-intelligence team. Their answers determine which feeds become `[READY]` for loader implementation (1 day each, following the F1 pattern).

---

## Email Draft (copy/paste into your mail client)

**To:** [procurement team / market-intel lead]
**Cc:** [analytics / IT]
**Subject:** 数据源盘点 — 预测系统需要的三类基本面数据

---

团队你好，

预测系统目前已完成技术底座（8 模型集成 + 走向前回测 + A/B 验证框架 + 决策 ROI 追踪），但有三类**基本面数据**仍是空白，而这恰恰是提升预测准确率天花板的关键。现有模型只能从历史价格外推，无法反映真实的供需格局。

麻烦帮忙盘点以下三类数据，**只要确认有订阅/有内部数据**，后续接入工作（ETL + 入库 + 特征工程）由我们承担，每类数据约 1 个工作日完成。

### 一、开工率数据（Operating Rate）

> 这是反映真实需求最及时的信号 —— 价格会骗人，开工率不会。

- [ ] 我司是否订阅了**隆众资讯**开工率数据？覆盖哪些产品？
- [ ] 是否订阅了**卓创资讯**开工率数据？覆盖哪些产品？
- [ ] 是否有**ICIS** / **Argus** / **Wood Mackenzie** 的开工率数据？
- [ ] 内部 ERP/MES 是否记录了**下游装置开工率**（如 SBS/SIS/SEBS 装置）？
- [ ] 数据频率：日 / 周 / 月？
- [ ] 数据格式：API / Excel 邮件 / 网页截图 / 数据库表？

### 二、港口与社会库存数据（Port & Social Inventory）

> 库存高位 + 价格上涨 = 虚高，必跌；库存低位 + 价格平稳 = 真底，必涨。

- [ ] 我司是否订阅了**隆众/卓创**的社会库存数据？覆盖哪些产品/港口？
- [ ] 是否有**港口库存**周报（如华东、华南主要港口）？
- [ ] 内部 ERP 是否记录了**我司自有库存**（按产品 + 仓库）？
- [ ] 数据频率：周 / 旬 / 月？
- [ ] 数据格式？

### 三、进口价格与到港数据（Import Price & Customs）

> 进口窗口打开 = 国内价格上行空间被封；进口窗口关闭 = 国内价格有上行空间。

- [ ] 我司是否订阅了**ICIS/Argus** 的 CFR/CIF 进口价？
- [ ] 是否有**海关总署**进口数据查询权限（公开数据，仅需 HS 编码映射）？
- [ ] 是否有**隆众/卓创**的进口船货到港预报？
- [ ] 内部 ERP 是否记录了**实际采购进口价**（按产品 + 抵港日）？
- [ ] 数据频率？

### 四、补充确认

- [ ] 现有**Wind / 同花顺 / Bloomberg** 终端权限中，是否有上述类别的数据可导出？
- [ ] 是否有**内部 MES** 记录了**产量**（按产品 + 装置 + 日）？这是最直接的供给端数据。
- [ ] 是否有**销售订单量**（按产品 + 日）的 ERP 表？我们已发现 `sale_erp_v_*` 表有此数据但未被预测系统使用。

---

确认任意一项"有"即可，我们会立即评估接入可行性。感谢！

---

## Internal Notes (for engineering)

- Questionnaire maps to `docs/superpowers/plans/2026-08-05-tier3-data-sourcing-checklist.md`
- Once a feed is `[READY]`: build loader following F1 pattern (`ErpVolumeLoader` is the template)
- Each loader: 1 day ETL + 1 day signal computation + 1 day flag-gated integration
- **Free data sources** (no procurement needed):
  - China customs import data (public) — needs HS code mapping (Stream C2)
  - Internal ERP sales volume (`sale_erp_v_*`) — already has loader, just needs flag ON
  - Internal MES production volumes — audit needed (Stream C3)
