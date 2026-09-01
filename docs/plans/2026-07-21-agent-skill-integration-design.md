# Agent Skill Integration Design — 2026-07-21

## Purpose

Enhance Zhanlu's Agent for enterprise applications by integrating a curated shortlist of best-in-class skills from three sources:

1. **Lark/Feishu** (larksuite/cli) — office suite integration (docs, sheets, slides, meeting digests)
2. **Notion** (makenotion/notion-cookbook + openai/skills) — knowledge management workflows
3. **Anthropic** (anthropics/skills) — brand consistency across artifacts

The skill set is intentionally small (8 skills) and complementary. Each one earns its place; no two skills do the same job.

## Current state

Zhanlu already has **45 skills** including all file-format ones (pptx, docx, xlsx-via-minimax, pdf, algorithmic-art, frontend-design, ui-ux-pro-max). The newly-added skills raise the count to **53**.

## The 8 new skills

| # | Skill | Source | Category | Why it's best-in-class |
|---|---|---|---|---|
| 1 | `lark-shared` | `larksuite/cli` | Auth infra | Required dependency for all other `lark-*` skills. Manages identity, scopes, error handling. |
| 2 | `lark-doc` | `larksuite/cli` | Documents | Full Docx/Wiki v2 editing in Feishu. DocxXML block-level precision. |
| 3 | `lark-sheets` | `larksuite/cli` | Spreadsheets | CRUD + pivot + charts + formulas + versioned history. |
| 4 | `lark-slides` | `larksuite/cli` | Presentations | Native Feishu slide deck creation with strict design standards. |
| 5 | `lark-workflow-meeting-summary` | `larksuite/cli` | **Headline** | Automated meeting digests: search → fetch → compile → publish. One command = weekly report. |
| 6 | `notion-knowledge-capture` | `makenotion/notion-cookbook` | Knowledge | Chat → structured Notion docs with smart links. Turns conversations into persistent knowledge. |
| 7 | `notion-meeting-intelligence` | `JetBrains/skills` (openai mirror) | Meetings | Meeting prep with research + agenda generation. |
| 8 | `brand-guidelines` | `anthropics/skills` | Branding | Enforces consistent enterprise visual identity across all generated artifacts. |

## Deliberately skipped (with reason)

- **Other 6 Lark skills** (`lark-drive`, `lark-minutes`, `lark-note`, `lark-wiki`, `lark-whiteboard`, `lark-markdown`) — subsumed by the 4 core doc skills; can add later if needed.
- **Other 12 Notion skills** (CRUD primitives + task workflow + cli) — low-level, redundant with Zhanlu's existing primitives.
- **anthropic xlsx** — already have `minimax-xlsx`.
- **anthropic frontend-design / algorithmic-art / webapp-testing / skill-creator** — already in Zhanlu.
- **anthropic theme-factory / doc-coauthoring / internal-comms** — overlapping with existing skills.
- **dbs-ai-check / dbs-report / dbs-wechat-html** — niche China-specific utilities.
- **design-doc-mermaid** — `lark-doc` already does whiteboards with Mermaid.

## Complementarity (how they work together)

```
User conversation
    ↓
[notion-meeting-intelligence] → pre-meeting research + agenda
    ↓
[lark-workflow-meeting-summary] → post-meeting digest → Feishu doc
    ↓
[notion-knowledge-capture] → key decisions archived to Notion
    ↓
[lark-doc / lark-sheets / lark-slides] → official deliverables published to Feishu
    ↓
[brand-guidelines] → all of the above visually consistent
```

This is a complete enterprise knowledge loop: **research → meeting → capture → publish → brand**.

## Integration architecture

```
/root/zhanlu/backend/skills/
├── lark-shared/                        ← auth foundation (new)
├── lark-doc/                           ← new
├── lark-sheets/                        ← new
├── lark-slides/                        ← new
├── lark-workflow-meeting-summary/      ← new (HEADLINE)
├── notion-knowledge-capture/           ← new
├── notion-meeting-intelligence/        ← new
├── brand-guidelines/                   ← new
└── ... (45 pre-existing skills unchanged)
```

## External dependencies (NOT auto-installed; user must set up)

| Dependency | What it is | How to set up | Required for |
|---|---|---|---|
| `lark-cli` | Official larksuite CLI talking to Feishu Open Platform | `npm i -g @larksuite/cli` then `lark-cli auth login --domain docs,sheets,slides,vc,drive` | `lark-*` (5 skills) |
| Notion MCP server | Local MCP server talking to Notion API | Install + `NOTION_API_KEY` env var | `notion-*` (2 skills) |
| Anthropic skill bodies | Self-contained SKILL.md (no external dep) | Nothing | `brand-guidelines` (1 skill) |

**Note on auth:** `lark-shared/SKILL.md` documents the full authentication protocol, including the split-flow device-code pattern for agent-mediated auth. Read it before the first `lark-cli auth login` call.

## File-by-file summary

```
lark-shared                       10,908 B   Auth + permissions protocol
lark-doc                           9,313 B   Feishu Docx/Wiki editing
lark-sheets                       37,173 B   Feishu Sheets (largest, most complex)
lark-slides                       26,515 B   Feishu Slides (design-heavy)
lark-workflow-meeting-summary      5,898 B   Meeting digest workflow
notion-knowledge-capture           7,233 B   Chat → Notion docs
notion-meeting-intelligence        3,418 B   Meeting prep
brand-guidelines                   2,235 B   Brand styling rules
                                ─────────
                                103,613 B   Total
```

## Verification

A smoke test script is provided at `/root/zhanlu/backend/scripts/verify_new_skills.py`. It:
1. Checks each skill directory exists under `backend/skills/`
2. Parses each SKILL.md frontmatter (`name`, `description`)
3. Confirms external dependencies are mentioned (`lark-cli`, Notion MCP)
4. Reports a pass/fail summary

Run it with:
```bash
python3 /root/zhanlu/backend/scripts/verify_new_skills.py
```

## Rollout plan

- **Phase 1 (this commit):** SKILL.md files in place. Skills are discoverable but not yet usable (no `lark-cli` auth, no Notion MCP).
- **Phase 2:** Install `lark-cli` + authenticate. Test `lark-workflow-meeting-summary` end-to-end on a real Feishu tenant.
- **Phase 3:** Install Notion MCP server + `NOTION_API_KEY`. Test `notion-knowledge-capture` end-to-end.
- **Phase 4:** Optional — pull in additional skills from the same registries as needed (`lark-drive`, `lark-minutes`, `lark-note`, `lark-wiki`, `lark-whiteboard`, `lark-markdown`, plus Notion CRUD primitives).

## Risks

- **Lark/Feishu tenant coupling:** the 5 `lark-*` skills only deliver value if Zhanlu is deployed against a Feishu tenant with appropriate scopes. If the target market is non-Feishu, these become dead weight.
- **Notion MCP server dependency:** the 2 `notion-*` skills require an external MCP server running locally. This is a new operational requirement not previously in Zhanlu's stack.
- **Skill selection noise:** with 53 skills, the agent's skill-selection step has more choices to consider. This is mitigated by clear namespacing (`lark-*` / `notion-*` / `brand-*`) and strong `description` frontmatter on each new skill.
- **`lark-shared` is a hard dependency:** if `lark-shared/SKILL.md` is moved or renamed, the other 4 `lark-*` skills will fail to load. Keep them co-located.

## References

- Lark skills source: https://github.com/larksuite/cli/tree/main/skills
- Notion knowledge-capture source: https://github.com/makenotion/notion-cookbook/tree/main/skills/claude/knowledge-capture
- Notion meeting-intelligence source: https://github.com/JetBrains/skills/tree/main/notion-meeting-intelligence (mirror at https://github.com/openai/skills)
- Brand-guidelines source: https://github.com/anthropics/skills/tree/main/skills/brand-guidelines
- mcpservers.org registry pages used for evaluation: see the original 18-URL list in the user request.
