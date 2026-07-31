# Automation Deliverables (Formats, Preview, Content Quality) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make automations produce useful multi-format deliverables (docx/pptx/pdf/md/xlsx/csv/json) chosen at creation time, previewable inline, with truthful content — and stop the creator-missing crash.

**Architecture:** Backend executor changes (prompt guidance, honesty guardrail, data-source preflight, crash fix) in `automation_executor.py`; frontend changes in the existing create dialog (`CreateResourceDialog.jsx`) and draft card (`AutomationDraftCard.jsx`). No new services, no new dependencies — the document generator and preview pipeline already exist.

**Tech Stack:** FastAPI/SQLAlchemy backend (uvicorn --reload in docker, source volume-mounted), React/Vite frontend (dist rebuilt via `npm run build`, served by /opt/zhanlu-serve.py behind nginx), pytest + SQLite test harness, vitest + RTL.

**Spec:** `docs/superpowers/specs/2026-07-28-automation-deliverables-design.md`

## Global Constraints

- Backend tests run ONLY via the SQLite harness (server RAM is limited; never run the full suite):
  `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/<file> -v`
- Frontend tests: `cd /root/zhanlu/frontend && npx vitest run <file>`.
- No new npm/pip dependencies.
- Format values are exactly: `html`, `docx`, `pptx`, `pdf`, `md`, `xlsx`, `csv`, `json`.
- The backend container auto-reloads on file change; the frontend requires `npm run build` to go live.
- Commit after each task.

---

### Task 1: Creator-missing crash fix (WS3)

A scheduled run whose task's `created_by_id` no longer resolves crashes the v3 stream with `'NoneType' object has no attribute 'id'`. Fix: fail the run cleanly with an ownership message.

**Files:**
- Modify: `backend/app/services/automation_executor.py` (`_run_agent_in_conversation` ~L895-905; `execute_automation` except chain ~L521)
- Test: `backend/tests/test_automation_creator_missing.py` (create)

**Interfaces:**
- Produces: `automation_executor._TaskCreatorMissingError(Exception)` — raised by `_run_agent_in_conversation` before any stream work when the creator can't be loaded; handled in `execute_automation` with `_mark_failed_no_retry`.

- [ ] **Step 1: Write the failing test**

```python
"""Regression: task creator user missing -> clean failure, no NoneType crash."""
import os, sys
import pytest

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


class _FakeDBNoUser:
    def get(self, model, key):
        return None  # creator user does not resolve


class _FakeTask:
    id = "task-1"
    name = "T"
    created_by_id = "ghost-user"
    session_id = None
    org_id = "default-org"
    app_id = "default-app"
    project_id = None


class _FakeAgent:
    id = "agent-1"
    name = "runtime"


def test_run_agent_in_conversation_raises_creator_missing():
    from app.services import automation_executor as ax
    with pytest.raises(ax._TaskCreatorMissingError):
        ax._run_agent_in_conversation(
            _FakeTask(), _FakeAgent(), "do things", "exec-1",
            db_override=_FakeDBNoUser(),
        )
```

Note: `_run_agent_in_conversation` currently opens its own `SessionLocal()`. The task adds an optional `db_override=None` parameter (test seam, default keeps production behavior) and uses it for the user lookup only.

- [ ] **Step 2: Run test to verify it fails**

Run: per Global Constraints with `tests/test_automation_creator_missing.py -v`
Expected: FAIL — `AttributeError: module has no attribute '_TaskCreatorMissingError'`

- [ ] **Step 3: Implement**

In `automation_executor.py`, near `_AutomationPaused`:

```python
class _TaskCreatorMissingError(Exception):
    """The task's created_by_id no longer resolves to a user row.

    Raised before the v3 stream is entered — the stream dereferences
    ``user.id`` on entry, so a missing creator otherwise crashes the run
    with an opaque ``'NoneType' object has no attribute 'id'``.
    """
```

Change the signature and user resolution in `_run_agent_in_conversation`:

```python
def _run_agent_in_conversation(
    task: AutomationTask,
    agent: AgentApp,
    prompt: str,
    execution_id: str = "",
    db_override=None,  # test seam: supply a fake for the user lookup
) -> Tuple[str, str, Optional[dict], dict]:
```

and right after `db = SessionLocal()` (keep it as the very first resolution step):

```python
        # Resolve the acting user BEFORE anything else: the v3 stream
        # dereferences user.id on entry, so a ghost creator would crash it.
        lookup_db = db_override if db_override is not None else db
        user = None
        if user_id:
            user = lookup_db.get(User, user_id)
        if user is None:
            raise _TaskCreatorMissingError(
                f"task creator user {user_id!r} no longer exists"
            )
```

(Remove the now-dead later duplicate `db.get(User, ...)` block.)

In `execute_automation`, add a dedicated except clause BEFORE the generic `except Exception`:

```python
        except _TaskCreatorMissingError as e:
            pool.shutdown(wait=False, cancel_futures=True)
            logger.warning("execute_automation: %s %s", execution_id, e)
            _mark_failed_no_retry(
                db, execution,
                "Task creator account no longer exists — re-save the "
                "automation to reassign ownership.",
            )
            return
```

- [ ] **Step 4: Run test to verify it passes**

Run: same harness command. Expected: 1 passed.
Also run `tests/test_automation_truthful_failure.py` (regression: 16 passed).

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/automation_executor.py backend/tests/test_automation_creator_missing.py && git commit -m "fix(automation): fail cleanly when task creator user is missing"
```

---

### Task 2: Format-aware prompt + honesty guardrail (WS2.1, WS2.2)

The run prompt never names the target format (pptx gets flat prose) and never forbids fabrication. Add a per-format guidance block + a guardrail sentence.

**Files:**
- Modify: `backend/app/services/automation_executor.py` (new module-level constants + helper above `execute_automation`; prompt assembly ~L481-490)
- Test: `backend/tests/test_automation_prompt_guidance.py` (create)

**Interfaces:**
- Produces: `automation_executor._format_guidance(output_format: str) -> str` and `automation_executor._HONESTY_GUARDRAIL: str`. Task 3's gate is independent; nothing consumes these outside the prompt builder.

- [ ] **Step 1: Write the failing test**

```python
"""Format guidance + honesty guardrail for the automation run prompt."""
import os, sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


def test_pptx_guidance_mentions_slides():
    from app.services.automation_executor import _format_guidance
    g = _format_guidance("pptx").lower()
    assert "slide" in g and "##" in g


def test_docx_guidance_mentions_sections_and_tables():
    from app.services.automation_executor import _format_guidance
    g = _format_guidance("docx").lower()
    assert "##" in g and "table" in g


def test_unknown_format_falls_back_to_html_guidance():
    from app.services.automation_executor import _format_guidance
    assert _format_guidance("weird") == _format_guidance("html")
    assert _format_guidance(None) == _format_guidance("html")


def test_guardrail_forbids_fabrication():
    from app.services.automation_executor import _HONESTY_GUARDRAIL
    g = _HONESTY_GUARDRAIL.lower()
    assert "never fabricate" in g
    assert "tools actually returned" in g


def test_prompt_builder_wires_guidance_and_guardrail():
    """Source-level: the prompt assembly must include both pieces."""
    import inspect
    from app.services import automation_executor as ax
    src = inspect.getsource(ax.execute_automation)
    assert "_format_guidance(" in src
    assert "_HONESTY_GUARDRAIL" in src
    assert "output_format" in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: per Global Constraints with `tests/test_automation_prompt_guidance.py -v`
Expected: FAIL — `ImportError: cannot import name '_format_guidance'`

- [ ] **Step 3: Implement**

In `automation_executor.py`, above `execute_automation`:

```python
_HONESTY_GUARDRAIL = (
    "Report only what tools actually returned. If a data source is "
    "missing, empty, or unreachable, say so explicitly and stop — never "
    "fabricate rows, totals, or 'successful sync' claims."
)

_FORMAT_GUIDANCE = {
    "html": (
        "Format: the deliverable is an HTML report. Use `##` section "
        "headings and markdown tables of real figures."
    ),
    "docx": (
        "Format: the deliverable is a Word document. Use `##` section "
        "headings, concise paragraphs, and markdown tables of real figures "
        "(tables become native Word tables)."
    ),
    "pptx": (
        "Format: the deliverable is a PowerPoint deck. Structure the answer "
        "as `## <slide title>` sections of short bullet points — each `##` "
        "heading becomes one slide, its bullets the slide body."
    ),
    "pdf": (
        "Format: the deliverable is a PDF document. Use `##` section "
        "headings and markdown tables of real figures."
    ),
    "md": (
        "Format: the deliverable is a Markdown document. Use `##` section "
        "headings and markdown tables of real figures."
    ),
    "xlsx": (
        "Format: the deliverable is an Excel workbook. Emit one JSON block "
        "of rows/records (no prose) so it can be converted to worksheets."
    ),
    "csv": (
        "Format: the deliverable is a CSV file. Emit CSV rows only — a "
        "header row plus data rows, no prose."
    ),
    "json": (
        "Format: the deliverable is a JSON document. Emit a single valid "
        "JSON object, no prose."
    ),
}


def _format_guidance(output_format: str) -> str:
    """Per-format output instruction for the run prompt — aligned with what
    document_generator's parsers expect for each output_format."""
    return _FORMAT_GUIDANCE.get(
        (output_format or "html").strip().lower(), _FORMAT_GUIDANCE["html"]
    )
```

In the `agent_prompt` assembly (replacing the fixed sentence):

```python
        agent_prompt = (
            f"{user_prompt}\n\n"
            f"## Automation context\n"
            f"You are running as part of the scheduled automation '{task.name}' "
            f"({task.id}). Produce a complete, well-structured response — the "
            f"system will save your output as a deliverable for the user.\n\n"
            + _format_guidance(getattr(task, "output_format", None))
            + "\n\n"
            + _HONESTY_GUARDRAIL
            + (f"\n\n{prev_context}" if prev_context else "")
            + (_memory_context or "")
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: same harness command. Expected: 5 passed.
Also run `tests/test_automation_truthful_failure.py` (16 passed) and `tests/test_automation_creator_missing.py` (1 passed).

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/automation_executor.py backend/tests/test_automation_prompt_guidance.py && git commit -m "feat(automation): format-aware run prompt + honesty guardrail"
```

---

### Task 3: data_sync data-source preflight (WS2.3)

A `data_sync` run with zero bound data sources can only produce fiction. Fail it before spending an LLM run, using the SAME binding resolution as the tool runtime.

**Files:**
- Modify: `backend/app/services/automation_executor.py` (new helper above `execute_automation`; gate after `_resolve_agent` ~L434-437)
- Test: `backend/tests/test_automation_ds_preflight.py` (create)

**Interfaces:**
- Consumes: `app.services.data_source_runtime.{get_bound_data_source_ids, _maybe_extend_with_workspace_auto_bind, _extend_with_project_kbs}` (existing).
- Produces: `automation_executor._resolve_bound_data_source_ids(db, agent, project_id) -> list[str]`.

- [ ] **Step 1: Write the failing test**

```python
"""data_sync preflight: zero bound data sources -> fail before the LLM run."""
import os, sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


def test_resolve_composes_all_three_binding_sources(monkeypatch):
    from app.services import automation_executor as ax
    calls = []

    def _own(agent):
        calls.append("own")
        return {"kb-own"}

    def _ws(db, agent, bound):
        calls.append("ws")
        return bound | {"kb-ws"}

    def _proj(db, agent, bound, project_id):
        calls.append("proj")
        assert project_id == "proj-1"
        return bound | {"kb-proj"}

    monkeypatch.setattr("app.services.data_source_runtime.get_bound_data_source_ids", _own)
    monkeypatch.setattr("app.services.data_source_runtime._maybe_extend_with_workspace_auto_bind", _ws)
    monkeypatch.setattr("app.services.data_source_runtime._extend_with_project_kbs", _proj)

    out = ax._resolve_bound_data_source_ids(db=None, agent=object(), project_id="proj-1")
    assert calls == ["own", "ws", "proj"], "must use the same resolution chain as the tool runtime"
    assert set(out) == {"kb-own", "kb-ws", "kb-proj"}


def test_gate_blocks_data_sync_without_sources_and_allows_others():
    """Source-level: the preflight gate sits before the agent invocation,
    applies only to data_sync, and fails retryably."""
    import inspect
    from app.services import automation_executor as ax
    src = inspect.getsource(ax.execute_automation)
    gate = src.index("_resolve_bound_data_source_ids")
    invoke = src.index("pool.submit(_run_agent_in_conversation")
    assert gate < invoke, "preflight must run before the LLM call"
    assert 'data_sync' in src
    # retryable failure (a binding may be added later) — not no_retry
    gate_region = src[gate:invoke]
    assert "_mark_failed(" in gate_region
    assert "_mark_failed_no_retry(" not in gate_region
```

- [ ] **Step 2: Run test to verify it fails**

Run: per Global Constraints with `tests/test_automation_ds_preflight.py -v`
Expected: FAIL — `AttributeError: module has no attribute '_resolve_bound_data_source_ids'`

- [ ] **Step 3: Implement**

In `automation_executor.py`, above `execute_automation`:

```python
def _resolve_bound_data_source_ids(db: Session, agent: AgentApp, project_id) -> list:
    """Effective bound data-source ids for a run — the SAME resolution
    chain the chat tool runtime uses, so the preflight can never disagree
    with what the agent's data tools would see."""
    from app.services import data_source_runtime as _dsrt
    bound = _dsrt.get_bound_data_source_ids(agent)
    bound = _dsrt._maybe_extend_with_workspace_auto_bind(db, agent, bound)
    bound = _dsrt._extend_with_project_kbs(db, agent, bound, project_id)
    return sorted(bound)
```

In `execute_automation`, immediately after the `_resolve_agent` success block:

```python
        # data_sync preflight: with zero bound data sources the run can
        # only fabricate content. Fail fast (retryable — a binding may be
        # added later) instead of spending an LLM run on fiction.
        if (task.type or "").strip().lower() == "data_sync":
            _bound_ids = _resolve_bound_data_source_ids(db, agent, task.project_id)
            if not _bound_ids:
                _mark_failed(
                    db, execution,
                    "No data source bound to this task's project/agent — "
                    "bind one in My Space → Data Agent, then re-run.",
                )
                return
```

- [ ] **Step 4: Run test to verify it passes**

Run: same harness command. Expected: 2 passed.
Also run `tests/test_automation_truthful_failure.py`, `tests/test_automation_prompt_guidance.py`, `tests/test_automation_creator_missing.py` (all green).

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/automation_executor.py backend/tests/test_automation_ds_preflight.py && git commit -m "feat(automation): preflight data_sync runs for bound data sources"
```

---

### Task 4: Output-format dropdown in the create dialog (WS1.1)

`CreateResourceDialog.jsx` builds the structured prefill. Add an Output format select (automation only) and emit it into the prefill so `create_automation` picks it up.

**Files:**
- Modify: `frontend/src/components/CreateResourceDialog.jsx` (state ~L92-101, reset effect ~L103-111, `buildPrefill()` ~L170-196, form JSX near the SchedulePicker block)
- Test: `frontend/src/components/CreateResourceDialog.test.jsx` (create)

**Interfaces:**
- Produces: prefill line `- Output format：Word document (docx)` (en) / `- 输出格式：Word document (docx)` — the parenthesized raw value lets `create_automation`'s `_detect_output_format` keyword-matching resolve it.
- Task 5's draft card parses the same line.

- [ ] **Step 1: Write the failing test**

```jsx
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const navigateMock = vi.fn();
vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
}));
vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({
    lang: 'en',
    t: new Proxy({}, { get: (o, k) => (o[k] ??= new Proxy({}, { get: () => '' })) }),
  }),
}));
vi.mock('@/api/base44Client', () => ({
  base44: { entities: { Project: { filter: vi.fn().mockResolvedValue([]) },
                        KnowledgeBase: { filter: vi.fn().mockResolvedValue([]) } } },
}));
vi.mock('@/components/automation/SchedulePicker', () => ({ default: () => null }));

const { default: CreateResourceDialog } = await import('@/components/CreateResourceDialog');

describe('CreateResourceDialog output format', () => {
  beforeEach(() => navigateMock.mockClear());

  it('emits the selected output format into the prefill', async () => {
    render(<CreateResourceDialog open onOpenChange={() => {}} resourceType="automation" />);
    fireEvent.change(screen.getByPlaceholderText(/name/i), { target: { value: 'Sales deck' } });
    // The format select defaults to HTML report; switch to Word document.
    fireEvent.click(screen.getByTestId('output-format-select'));
    fireEvent.click(await screen.findByText('Word document'));
    fireEvent.click(screen.getByRole('button', { name: /create|submit|ok/i }));
    await waitFor(() => expect(navigateMock).toHaveBeenCalled());
    const url = navigateMock.mock.calls[0][0];
    const prefill = decodeURIComponent(new URLSearchParams(url.split('?')[1]).get('prefill'));
    expect(prefill).toContain('Output format');
    expect(prefill).toContain('docx');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/CreateResourceDialog.test.jsx`
Expected: FAIL — `Unable to find element by testid "output-format-select"`

- [ ] **Step 3: Implement**

In `CreateResourceDialog.jsx`:

Add near the state block:

```jsx
const OUTPUT_FORMAT_OPTIONS = [
  { value: 'html', label: 'HTML report' },
  { value: 'docx', label: 'Word document' },
  { value: 'pptx', label: 'PowerPoint deck' },
  { value: 'pdf', label: 'PDF document' },
  { value: 'md', label: 'Markdown' },
  { value: 'xlsx', label: 'Excel workbook' },
  { value: 'csv', label: 'CSV' },
  { value: 'json', label: 'JSON' },
];
```

```jsx
const [outputFormat, setOutputFormat] = useState('html');
```

Add `setOutputFormat('html');` to the open-reset effect. In `buildPrefill()`, after the schedule line:

```jsx
    if (resourceType === 'automation') {
      const opt = OUTPUT_FORMAT_OPTIONS.find((o) => o.value === outputFormat) || OUTPUT_FORMAT_OPTIONS[0];
      lines.push(`- ${isEn ? 'Output format' : '输出格式'}：${opt.label} (${opt.value})`);
    }
```

In the form JSX (directly after the SchedulePicker block, automation only):

```jsx
        {resourceType === 'automation' && (
          <div className="space-y-2">
            <Label>{isEn ? 'Output format' : '输出格式'}</Label>
            <Select value={outputFormat} onValueChange={setOutputFormat}>
              <SelectTrigger data-testid="output-format-select">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {OUTPUT_FORMAT_OPTIONS.map((o) => (
                  <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}
```

- [ ] **Step 4: Run test to verify it passes**

Run: same vitest command. Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/CreateResourceDialog.jsx frontend/src/components/CreateResourceDialog.test.jsx && git commit -m "feat(automation): output-format picker in create dialog"
```

---

### Task 5: Format field in the draft card (WS1.2)

The chat draft card parses the prefill and lets the user edit fields. Add the output format as a parsed, Select-editable field so users see (and can change) what they'll get before confirming.

**Files:**
- Modify: `frontend/src/components/chat/AutomationDraftCard.jsx` (`parsePrefill` ~L377+, field grid ~L178+)
- Test: `frontend/src/components/chat/AutomationDraftCard.test.jsx` (create)

**Interfaces:**
- Consumes: the `- Output format：<label> (<value>)` prefill line from Task 4.
- Produces: `parsePrefill` result gains `outputFormat: string` (raw value e.g. `'docx'`, default `'html'`).

- [ ] **Step 1: Write the failing test**

```jsx
import { describe, it, expect, vi } from 'vitest';

vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({
    lang: 'en',
    t: new Proxy({}, { get: (o, k) => (o[k] ??= new Proxy({}, { get: () => '' })) }),
  }),
}));
vi.mock('@/components/automation/SchedulePicker', () => ({ default: () => null }));

const { parsePrefill } = await import('@/components/chat/AutomationDraftCard');

describe('AutomationDraftCard parsePrefill output format', () => {
  it('extracts the raw format value from the prefill line', () => {
    const prefill = [
      'Create a new Automation Task:',
      '- Name: Sales deck',
      '- Type: report_generation',
      '- Output format：Word document (docx)',
      '- Description: weekly deck',
    ].join('\n');
    const parsed = parsePrefill(prefill, {}, true);
    expect(parsed.outputFormat).toBe('docx');
  });

  it('defaults to html when the line is absent', () => {
    const parsed = parsePrefill('Create a new Automation Task:\n- Name: X', {}, true);
    expect(parsed.outputFormat).toBe('html');
  });
});
```

Note: `parsePrefill` is currently module-private — this task also exports it (named export alongside the default component export).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/chat/AutomationDraftCard.test.jsx`
Expected: FAIL — `parsePrefill` is not exported / `outputFormat` undefined.

- [ ] **Step 3: Implement**

In `AutomationDraftCard.jsx`:

Export the parser: change `function parsePrefill(` to `export function parsePrefill(`.

Add to the parser (alongside the other `- <label>：` line matches, both en label `Output format` and zh `输出格式`), capturing the raw value inside the trailing parentheses:

```jsx
  const fmtMatch = raw.match(/-\s*(?:Output format|输出格式)[：:]\s*.*?\((\w+)\)\s*$/m);
  out.outputFormat = fmtMatch ? fmtMatch[1].toLowerCase() : 'html';
```

Add a format Select row in the field grid (after the Type `Field`), using the same 8 options as the dialog; committing via `commitEdit('outputFormat', v)` so edits flow into the existing "Updated: …" summary machinery:

```jsx
        <div className="flex items-start gap-2" data-field="output-format">
          <ListOrdered className="mt-1 h-3.5 w-3.5 text-muted-foreground" />
          <div className="min-w-0 flex-1">
            <div className="text-xs text-muted-foreground">{isEn ? 'Output format' : '输出格式'}</div>
            <Select value={draft.outputFormat || 'html'} onValueChange={(v) => commitEdit('outputFormat', v)}>
              <SelectTrigger className="h-7 text-sm"><SelectValue /></SelectTrigger>
              <SelectContent>
                {['html','docx','pptx','pdf','md','xlsx','csv','json'].map((f) => (
                  <SelectItem key={f} value={f}>{f}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
```

- [ ] **Step 4: Run test to verify it passes**

Run: same vitest command. Expected: 2 passed. Also `npx vitest run src/pages/Chat.test.jsx` (5 passed — no regression).

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/chat/AutomationDraftCard.jsx frontend/src/components/chat/AutomationDraftCard.test.jsx && git commit -m "feat(automation): editable output format in draft card"
```

---

### Task 6: Live verification (WS1.3)

End-to-end proof on the deployed site. Backend changes auto-reload; frontend changes require a rebuild first.

**Files:** none (verification only).

- [ ] **Step 1: Rebuild the frontend**

```bash
cd /root/zhanlu/frontend && npm run build 2>&1 | tail -3
```
Expected: `✓ built in ~10s`, new `dist/assets/index-*.js`.

- [ ] **Step 2: Create a docx automation via the dialog and run it**

With playwright-cli (session already logged in as admin@zhanlu.dev):
1. `goto http://zhanlu.ai:8000/automation` → New Task → fill name "Format Check Deck", pick **Word document** in the format select, submit.
2. Confirm the draft card in chat shows the format field = `docx`.
3. Confirm the created task (`docker exec zhanlu-postgres psql -U zhanlu -d zhanlu -c "SELECT name, output_format FROM automation_tasks ORDER BY created_date DESC LIMIT 1;"`) shows `docx`.
4. Automation page → Run → wait ~2 min.

Expected: the chat card for the run shows a "Word document" artifact; expanding the inline preview renders converted content (not download-only).

- [ ] **Step 3: Verify truthfulness still holds**

The "Daily Sales Data Sync" `data_sync` task (no bound sources) — run it once more.
Expected: the run fails fast with "No data source bound…" (preflight), no LLM steps spent, and the chat shows the ⚠️ failure notification with the config guidance.

- [ ] **Step 4: Commit**

Nothing to commit (verification only). Record results in the session memory.

---

## Self-Review

- **Spec coverage**: WS1.1→Task 4, WS1.2→Task 5, WS1.3→Task 6, WS2.1+2.2→Task 2, WS2.3→Task 3, WS3→Task 1. All covered.
- **Type consistency**: `_resolve_bound_data_source_ids(db, agent, project_id) -> list` used identically in Task 3 test + impl; `_format_guidance(str) -> str` identical in Task 2 test + impl; `outputFormat` (camelCase) identical across Tasks 4-5; prefill line format identical across Tasks 4-5.
- **Placeholders**: none — every step has runnable code/commands.
