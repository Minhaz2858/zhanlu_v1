# Ecisco BI Agent Migration Plan

**Date**: 2026-08-03  
**Status**: Complete  
**Source**: EDIA 5.1.2 → Zhanlu 7.30

---

## Overview

Migrate all 5 EDIA module groups into Zhanlu's Ecisco BI as a single `ecisco_bi_assistant` agent, replacing the older `orchestrator_agent`.

### Key Decisions
- **Rename**: `orchestrator_agent` → `ecisco_bi_assistant` with idempotent dedup logic
- **Routing**: Hybrid — direct Python calls for fast paths, delegation tools for complex reasoning
- **Defaults**: All 12 C5/C9 products supported by default
- **Feature flags**: 7 `ECISCO_BI_*_ENABLED` flags gate each module group (default `False`)

---

## Implementation Plan

### Phase 1: Rename + Migrate Existing Agent
- [x] Rename `orchestrator_agent` → `ecisco_bi_assistant` in `system_agents.py`
- [x] Update `agent_definitions/__init__.py` (prompt + AgentDefinition)
- [x] Alembic migration `036_rename_ecisco_bi_agent.py`
- [x] Update `seed.py`, `entity_service.py`, all test references
- [x] Frontend auto-select in `Chat.jsx`

### Phase 2: 5A — Knowledge Graph
**EDIA Source**: `EDIA_5.1.2/backend/src/domain/knowledge_graph.py`, `edia_ontology.py`, `product_resolver.py`

**Zhanlu Files**:
| File | Purpose |
|---|---|
| `backend/app/services/knowledge_graph/models.py` | ProductNode, Relationship, KnowledgeGraph |
| `backend/app/services/knowledge_graph/graph.py` | C5/C9 graph: 13 nodes, 18 edges (PRODUCES/CONSUMES/SUBSTITUTE) |
| `backend/app/services/knowledge_graph/resolver.py` | 3-phase product resolver (exact → shortest → longest) |
| `backend/app/services/knowledge_graph/tool.py` | `ask_knowledge_graph` handler |

**Adaptation Notes**: Class-based constructor; BFS multi-hop via `_multi_hop_traverse()`.

### Phase 3: 5B — Pricing + Decision
**EDIA Source**: `pricing_service`, `decision_service`, `decision_critic`, `decision_audit`, `macro_override`

**Zhanlu Files**:
| File | Purpose |
|---|---|
| `backend/app/services/pricing/service.py` | 9-step pricing ladder |
| `backend/app/services/pricing/macro_override.py` | Naphtha shock injection (±4% cap), file-based JSON store |
| `backend/app/services/pricing/tool.py` | `ask_pricing` + `ask_macro_override` handlers |
| `backend/app/services/decision/service.py` | Pipeline assembly (perception→diagnosis→forecast→pricing→critic) |
| `backend/app/services/decision/critic.py` | 6 flag types (contradiction, overconfidence, etc.) |
| `backend/app/services/decision/audit.py` | SHA-256 reasoning hashing, in-memory audit trail |
| `backend/app/services/decision/tool.py` | `ask_decision` handler |

### Phase 4: 5C — Perception + Diagnosis
**EDIA Source**: Decision graph stage 1-2 (perception, resolve_product, diagnosis)

**Zhanlu Files**:
| File | Purpose |
|---|---|
| `backend/app/services/perception/service.py` | Market snapshot, IsolationForest anomaly detection |
| `backend/app/services/perception/tool.py` | `ask_perception` handler |
| `backend/app/services/diagnosis/service.py` | BFS multi-hop causal chain, driver ranking, divergence detection |
| `backend/app/services/diagnosis/tool.py` | `ask_diagnosis` handler |

### Phase 5: 5D — Intelligence Enhancements
**EDIA Source**: `intelligence_learning_loop`, `narrative_generator`, `product_intelligence`

**Zhanlu Files**:
| File | Purpose |
|---|---|
| `backend/app/services/intelligence/learning_loop.py` | Outcome tracking, elasticity calibration |
| `backend/app/services/intelligence/narrative.py` | 6 event-type prompt templates |
| `backend/app/services/intelligence/product_card.py` | Per-product signal aggregation |

### Phase 6: 5E — Weekly Report
**EDIA Source**: `weekly_report_generator`, `weekly_report_charts`, `weekly_report_data`, `weekly_report_parser`

**Zhanlu Files**:
| File | Purpose |
|---|---|
| `backend/app/services/weekly_report/generator.py` | 6-step pipeline (resolve week → load catalog → macro → per-product → summary → assemble) |
| `backend/app/services/weekly_report/data.py` | ISO week resolution, number collection |
| `backend/app/services/weekly_report/charts.py` | Chart stub (matplotlib not in container) |
| `backend/app/services/weekly_report/parser.py` | Markdown section extraction |
| `backend/app/services/weekly_report/tool.py` | `ask_weekly_report` handler |

### Phase 7: 5F — ERP Writeback
**EDIA Source**: `erp_kpi_service`, `erp_writeback_service`

**Zhanlu Files**:
| File | Purpose |
|---|---|
| `backend/app/services/erp/kpi.py` | Product SKU mappings, KPI queries |
| `backend/app/services/erp/writeback.py` | Stage/approve pricing recs, human-in-the-loop |
| `backend/app/services/erp/tool.py` | `ask_erp_kpi` + `ask_erp_writeback` handlers |

---

## Wiring

### Delegation Tools
`backend/app/services/tool_handlers/edia_delegation_tools.py`:
- 12 tools total (6 original + 6 new faster direct-call replacements)
- `_ask_knowledge_graph`, `_ask_decision`, `_ask_macro_override`, `_ask_weekly_report`, `_ask_erp_kpi`, `_ask_erp_writeback` use direct Python service calls

### System Agent
`backend/app/services/system_agents.py`:
- `ecisco_bi_assistant` with 14 enabled tools
- `ensure_system_agents()` dedup on startup

### Feature Flags
`backend/app/config.py`:
```
ECISCO_BI_KNOWLEDGE_GRAPH_ENABLED = False
ECISCO_BI_PRICING_ENABLED = False
ECISCO_BI_DECISION_ENABLED = False
ECISCO_BI_PERCEPTION_ENABLED = False
ECISCO_BI_DIAGNOSIS_ENABLED = False
ECISCO_BI_WEEKLY_REPORT_ENABLED = False
ECISCO_BI_ERP_WRITEBACK_ENABLED = False
```

### Frontend
`frontend/src/pages/Chat.jsx`:
- URL params: `?project=Ecisco BI` auto-selects `ecisco_bi_assistant`
- `handleSend` fallback when `pendingProject === 'Ecisco BI'`

---

## EDIA → Zhanlu Adaptation Patterns

| EDIA Pattern | Zhanlu Adaptation |
|---|---|
| Module-level singletons | Class-based constructors |
| Raw `text()` SQL | Async SQLAlchemy |
| Threading | asyncio |
| Sub-agent delegation (slow) | Direct Python service calls (fast path) |
| `graph.upstream_of(max_hops=3)` | Manual BFS `_multi_hop_traverse()` |
| `graph.get_all_ids()` | `[n.id for n in graph.nodes]` |

---

## Test Results

- **99/99 tests pass** (`test_edia_migration.py`)
- All renamed tests pass (8 TestSystemAgent + 27 TestAgentDefinitions)
- Knowledge graph resolver tests pass (isoprene, piperylene, dcpd, etc.)
- Pricing ladder returns correct action/price/range/confidence with mock data
- Weekly report generator produces valid markdown for all 12 products
- All modules verified: import and execute correctly with mock data

---

## Known Limitations

1. **Feature flags all default False** — must enable per-module for production use
2. **Weekly report charts** — stub only (matplotlib not in Docker container)
3. **ERP KPI queries** — stub until MySQL writeback wired
4. **MySQL dependency** — all services use mock data when `EDIA_MYSQL_URL` is empty

---

## Restart Required

After any service code change:
```bash
cd /home/ysk2025/zhanlu_7_30 && docker compose restart backend
```
No `--reload` on uvicorn.
