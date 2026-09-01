import { useState, useEffect, useMemo, useRef } from 'react';
import { Pencil, X, Send, RotateCcw, Check, Clock, FolderOpen, Database, Tag, Type, ListChecks, ChevronDown, ChevronUp, ListOrdered, AlertCircle } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import SchedulePicker from '@/components/automation/SchedulePicker';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { nextRuns, formatRun } from '@/lib/nextRuns';

/**
 * AutomationDraftCard — sticky, editable summary of an in-flight
 * automation task right after the user submits the creation dialog.
 *
 * Parses the prefill text into structured fields (name, type,
 * schedule, project, data sources, description) and lets the user
 * edit any field inline. Edits are tracked locally and emitted as
 * short "Updated: <field> = <value>" lines via `onAppendToInput`,
 * so the user always reviews the final message before the agent
 * sees it. The card stays in the chat as a reference until the
 * user dismisses it.
 */
export default function AutomationDraftCard({ prefill, lang: langProp, onAppendToInput, onDismiss, onRetry, error }) {
  const { t, lang: langCtx } = useLanguage();
  const lang = langProp || langCtx;
  const isEn = lang === 'en';
  const c = t.createDialog.draftCard;
  const lbl = c.field;

  const parsed = useMemo(() => parsePrefill(prefill, t, isEn), [prefill, t, isEn]);

  // Local editable copy. Initialized from the parsed prefill; the
  // user can override any field.
  const [draft, setDraft] = useState(parsed);
  useEffect(() => setDraft(parsed), [parsed]);
  const [editing, setEditing] = useState(null);
  const [collapsed, setCollapsed] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);

  // Pending edits: { fieldKey: { from, to } }
  const changes = useMemo(() => {
    const out = {};
    Object.keys(parsed).forEach((k) => {
      if (k === 'raw' || k === 'availableDataSources') return;
      const from = parsed[k];
      const to = draft[k];
      if (Array.isArray(from) && Array.isArray(to)) {
        if (from.join('|') !== to.join('|')) out[k] = { from: from.join('、'), to: to.join('、') };
      } else if ((from || '') !== (to || '')) {
        out[k] = { from, to };
      }
    });
    return out;
  }, [parsed, draft]);

  const changeCount = Object.keys(changes).length;

  function startEdit(key) { setEditing(key); }
  function cancelEdit() { setEditing(null); }
  function commitEdit(key, value) {
    setDraft((d) => ({ ...d, [key]: value }));
    setEditing(null);
  }

  function resetAll() { setDraft(parsed); setEditing(null); }

  function buildEditSummary() {
    if (changeCount === 0) return '';
    const lines = isEn
      ? ['Please update the automation with these changes:']
      : ['请按以下最新信息更新自动化任务：'];
    Object.entries(changes).forEach(([key, { to }]) => {
      const label = lbl[key] || key;
      lines.push(`- ${label}: ${to}`);
    });
    return lines.join('\n');
  }

  function sendToAgent() {
    if (changeCount === 0) return;
    const text = buildEditSummary();
    if (typeof onAppendToInput === 'function') onAppendToInput(text);
  }

  if (collapsed) {
    return (
      <div className="mx-auto mb-3 flex w-full max-w-3xl items-center justify-between rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
        <button
          type="button"
          onClick={() => setCollapsed(false)}
          className="inline-flex items-center gap-1.5 transition-colors hover:text-foreground"
        >
          <ChevronDown className="h-3.5 w-3.5" />
          {c.title}
          {changeCount > 0 && (
            <span className="rounded-full bg-primary px-1.5 py-px text-[10px] font-medium text-primary-foreground">
              {changeCount}
            </span>
          )}
        </button>
        {typeof onDismiss === 'function' && (
          <button
            type="button"
            onClick={onDismiss}
            title={c.dismiss}
            className="rounded p-1 transition-colors hover:bg-muted hover:text-foreground"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="mx-auto mb-3 w-full max-w-3xl overflow-hidden rounded-xl border border-border bg-card shadow-sm">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border bg-muted/30 px-4 py-2.5">
        <div className="flex items-center gap-2">
          <span className="flex h-7 w-7 items-center justify-center rounded-md bg-primary/10 text-primary">
            <Clock className="h-3.5 w-3.5" />
          </span>
          <div className="min-w-0">
            <div className="flex items-center gap-1.5">
              <span className="text-sm font-medium text-foreground">{c.title}</span>
              {changeCount > 0 && (
                <span className="rounded-full bg-primary px-1.5 py-px text-[10px] font-medium text-primary-foreground">
                  {changeCount}
                </span>
              )}
            </div>
            <p className="truncate text-[11px] text-muted-foreground">{c.subtitle}</p>
          </div>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => setCollapsed(true)}
            title={c.dismiss}
            className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <ChevronUp className="h-3.5 w-3.5" />
          </button>
          {typeof onDismiss === 'function' && (
            <button
              type="button"
              onClick={onDismiss}
              title={c.dismiss}
              className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </div>

      {/* Error banner — shown when the agent failed to create the
          task (e.g. it tried to write a file instead of using
          `create_resource`). Gives the user a clear retry path. */}
      {error && (
        <div className="flex items-start gap-2 border-b border-destructive/30 bg-destructive/5 px-4 py-2.5 text-xs text-destructive">
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <div className="min-w-0 flex-1">
            <div className="font-medium">{error}</div>
            <div className="mt-0.5 text-destructive/80">
              {isEn ? 'You can retry by editing fields and clicking "Send to Agent", or click Retry below to resend the original prompt.' : '可点击下方"重试"按原内容重新发送，或编辑字段后点"发送到 Agent"。'}
            </div>
          </div>
          {typeof onRetry === 'function' && (
            <Button size="sm" variant="outline" onClick={onRetry} className="h-6 shrink-0 border-destructive/30 px-2 text-[11px] text-destructive hover:bg-destructive/10">
              <RotateCcw className="h-3 w-3" /> {isEn ? 'Retry' : '重试'}
            </Button>
          )}
        </div>
      )}

      {/* Field grid */}
      <div className="grid grid-cols-1 gap-x-6 gap-y-2 px-4 py-3 text-sm sm:grid-cols-2">
        <Field
          icon={Type}
          label={lbl.name}
          value={draft.name}
          editing={editing === 'name'}
          onStartEdit={() => startEdit('name')}
          onCancel={cancelEdit}
          onCommit={(v) => commitEdit('name', v)}
          changed={Boolean(changes.name)}
          placeholder={t.createDialog.namePh}
          lang={lang}
        />
        <Field
          icon={Tag}
          label={lbl.type}
          value={draft.type}
          editing={editing === 'type'}
          onStartEdit={() => startEdit('type')}
          onCancel={cancelEdit}
          onCommit={(v) => commitEdit('type', v)}
          changed={Boolean(changes.type)}
          placeholder={isEn ? 'data_sync / report_generation / …' : 'data_sync / report_generation / …'}
          lang={lang}
        />
        <div className="flex items-start gap-2" data-field="output-format">
          <Tag className="mt-1 h-3.5 w-3.5 text-muted-foreground" />
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
        <Field
          icon={Clock}
          label={lbl.schedule}
          value={draft.schedule}
          editing={editing === 'schedule' || pickerOpen}
          onStartEdit={() => { setPickerOpen(true); setEditing('schedule'); }}
          onCancel={() => { setPickerOpen(false); cancelEdit(); }}
          onCommit={(v) => { setPickerOpen(false); commitEdit('schedule', v); }}
          changed={Boolean(changes.schedule)}
          extra={pickerOpen ? (
            <div className="mt-2 rounded-md border border-border bg-muted/30 p-2">
              <SchedulePicker value={draft.schedule} onChange={(v) => setDraft((d) => ({ ...d, schedule: v }))} />
              <div className="mt-2 flex justify-end gap-2">
                <Button size="sm" variant="outline" onClick={() => { setPickerOpen(false); cancelEdit(); setDraft((d) => ({ ...d, schedule: parsed.schedule })); }}>
                  {t.createDialog.cancel}
                </Button>
                <Button size="sm" onClick={() => { setPickerOpen(false); commitEdit('schedule', draft.schedule); }}>
                  <Check className="h-3.5 w-3.5" /> {isEn ? 'OK' : '确定'}
                </Button>
              </div>
            </div>
          ) : null}
          placeholder="—"
          lang={lang}
        />
        {/* Next-runs preview — sits on its own row under the schedule
            field so the user can see what the rule will fire on. Hidden
            when the picker is open (the picker already shows its own
            next-runs list) or when the schedule is invalid. */}
        {!pickerOpen && draft.schedule && (
          <NextRunsRow
            scheduleString={draft.schedule}
            label={c.nextRuns}
            empty={c.nextRunsEmpty}
            more={t.createDialog.nextRuns.more}
            isEn={isEn}
            hint={c.nextRunsHint}
          />
        )}
        <Field
          icon={FolderOpen}
          label={lbl.project}
          value={draft.project}
          editing={editing === 'project'}
          onStartEdit={() => startEdit('project')}
          onCancel={cancelEdit}
          onCommit={(v) => commitEdit('project', v)}
          changed={Boolean(changes.project)}
          placeholder="—"
          lang={lang}
        />
        <div className="sm:col-span-2">
          <Field
            icon={Database}
            label={lbl.dataSources}
            value={draft.dataSources.join(isEn ? ', ' : '、')}
            editing={editing === 'dataSources'}
            onStartEdit={() => startEdit('dataSources')}
            onCancel={cancelEdit}
            onCommit={(v) => commitEdit('dataSources', v.split(/[,、]/).map((s) => s.trim()).filter(Boolean))}
            changed={Boolean(changes.dataSources)}
            placeholder={t.createDialog.dataSourcesEmpty}
            multiline
            lang={lang}
          />
        </div>
        <div className="sm:col-span-2">
          <Field
            icon={ListChecks}
            label={lbl.description}
            value={draft.description}
            editing={editing === 'description'}
            onStartEdit={() => startEdit('description')}
            onCancel={cancelEdit}
            onCommit={(v) => commitEdit('description', v)}
            changed={Boolean(changes.description)}
            placeholder={t.createDialog.descPh}
            multiline
            lang={lang}
          />
        </div>
      </div>

      {/* Footer / actions */}
      <div className="flex items-center justify-between gap-3 border-t border-border bg-muted/20 px-4 py-2.5">
        <p className="truncate text-[11px] text-muted-foreground">
          {changeCount > 0 ? c.sendHint : c.changesEmpty}
        </p>
        <div className="flex items-center gap-2">
          {changeCount > 0 && (
            <Button size="sm" variant="ghost" onClick={resetAll} className="h-7 px-2 text-xs">
              <RotateCcw className="h-3 w-3" /> {isEn ? 'Reset' : '重置'}
            </Button>
          )}
          <Button size="sm" onClick={sendToAgent} disabled={changeCount === 0} className="h-7 px-3 text-xs">
            <Send className="h-3 w-3" /> {c.sendToAgent}
          </Button>
        </div>
      </div>
    </div>
  );
}

function Field({ icon: Icon, label, value, editing, onStartEdit, onCancel, onCommit, changed, placeholder, multiline, extra, lang }) {
  const isEn = lang === 'en';
  return (
    <div className="min-w-0">
      <div className="mb-0.5 flex items-center gap-1 text-[11px] uppercase tracking-wide text-muted-foreground">
        <Icon className="h-3 w-3" /> {label}
      </div>
      {!editing ? (
        <button
          type="button"
          onClick={onStartEdit}
          className={
            'group flex w-full items-start gap-1.5 rounded-md px-1.5 py-1 -mx-1.5 text-left transition-colors hover:bg-muted ' +
            (changed ? 'ring-1 ring-primary/40 bg-primary/5' : '')
          }
          title={isEn ? 'Click to edit' : '点击编辑'}
        >
          <span className={'min-w-0 flex-1 truncate ' + (value ? 'text-foreground' : 'text-muted-foreground')}>
            {value || <span className="italic">{placeholder}</span>}
          </span>
          <Pencil className="h-3 w-3 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
        </button>
      ) : (
        <div className="-mx-1.5">
          {extra ? extra : (
            <InlineEditor
              value={value || ''}
              placeholder={placeholder}
              multiline={multiline}
              onCancel={onCancel}
              onCommit={onCommit}
            />
          )}
        </div>
      )}
    </div>
  );
}

function InlineEditor({ value, placeholder, multiline, onCancel, onCommit }) {
  const [v, setV] = useState(value);
  const ref = useRef(null);
  useEffect(() => { ref.current?.focus(); ref.current?.select?.(); }, []);
  function commit() { onCommit(v.trim()); }
  return (
    <div className="rounded-md ring-1 ring-primary/40 bg-card">
      {multiline ? (
        <Textarea
          ref={ref}
          value={v}
          onChange={(e) => setV(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Escape') onCancel(); if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) commit(); }}
          placeholder={placeholder}
          rows={3}
          className="resize-none border-0 focus-visible:ring-0"
        />
      ) : (
        <Input
          ref={ref}
          value={v}
          onChange={(e) => setV(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Escape') onCancel(); if (e.key === 'Enter') commit(); }}
          placeholder={placeholder}
          className="border-0 focus-visible:ring-0"
        />
      )}
      <div className="flex items-center justify-end gap-1 border-t border-border bg-muted/30 px-1.5 py-1">
        <Button size="sm" variant="ghost" onClick={onCancel} className="h-6 px-2 text-[11px]">
          {multiline ? 'Esc' : 'Esc'}
        </Button>
        <Button size="sm" onClick={commit} className="h-6 px-2 text-[11px]">
          <Check className="h-3 w-3" /> OK
        </Button>
      </div>
    </div>
  );
}

/**
 * Parse the dialog-emitted prefill into a structured draft. Format:
 *   帮我新建一个自动化任务：
 *   - 名称：xxx
 *   - 类型：xxx
 *   - 调度规则：xxx
 *   - 所属项目：xxx
 *   - 可用数据源：a、b、c
 *   - 描述：xxx
 *
 * Anything missing is just empty; the card renders a placeholder.
 */
export function parsePrefill(text, t, isEn) {
  const out = { raw: text || '', name: '', type: '', schedule: '', project: '', dataSources: [], description: '', outputFormat: 'html' };
  if (!text) return out;
  const lines = text.split(/\r?\n/);
  const sep = isEn ? /:\s*|：\s*/ : /：\s*|:\s*/;
  for (const line of lines) {
    const m = line.match(/^\s*[-•]\s*([^：:]+?)\s*[：:]\s*(.*)$/);
    if (!m) continue;
    const key = m[1].trim();
    const val = (m[2] || '').trim();
    const nameKey = t.createDialog.name;
    const typeKey = t.createDialog.type;
    const schedKey = t.createDialog.schedule;
    const projKey = t.createDialog.project;
    const dsKey = t.createDialog.dataSourcesLabel;
    const descKey = t.createDialog.description;
    if (key === nameKey) out.name = val;
    else if (key === typeKey) out.type = val;
    else if (key === schedKey) out.schedule = val;
    else if (key === projKey) out.project = val;
    else if (key === dsKey) out.dataSources = splitList(val, isEn);
    else if (key === descKey) out.description = val;
    // Output format line: "- Output format：Word document (docx)" — the
    // raw value lives in the trailing parentheses (dialog emits it).
    else if (key === 'Output format' || key === '输出格式') {
      const vm = val.match(/\((\w+)\)\s*$/);
      out.outputFormat = vm ? vm[1].toLowerCase() : (val || 'html');
    }
  }
  return out;
}

function splitList(val, isEn) {
  if (!val) return [];
  // Use both Chinese and English separators, plus optional "and more" tail.
  const cleaned = val.replace(/（.*?）|\(.*?\)/g, '').replace(/等\s*\d+/g, '').replace(/and\s+\d+\s+more/i, '');
  return cleaned.split(/[、,，;；]/).map((s) => s.trim()).filter(Boolean);
}

/**
 * Compact next-runs strip shown on the draft card. Computes up to 4
 * upcoming run times from the current schedule string and renders
 * them as a single inline line: "Next runs: Mon 28 Jul 09:00 (3
 * more)". Falls back to an italic empty message when no runs match.
 */
function NextRunsRow({ scheduleString, label, empty, more, isEn, hint }) {
  const runs = useMemo(() => nextRuns(scheduleString, 4), [scheduleString]);
  if (!runs.length) {
    return (
      <div className="sm:col-span-2 -mt-1 flex flex-wrap items-center gap-1.5 text-[11px] italic text-muted-foreground/80">
        <ListOrdered className="h-3 w-3" />
        {empty}
      </div>
    );
  }
  const [first, ...rest] = runs;
  const extra = runs.length - 1;
  return (
    <div className="sm:col-span-2 -mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-[11px]">
      <ListOrdered className="h-3 w-3 text-primary" />
      <span className="font-medium text-foreground/70">{label}:</span>
      <span className="font-mono text-foreground">{formatRun(first, isEn)}</span>
      {extra > 0 && <span className="text-muted-foreground/80">({more} {extra})</span>}
      {hint && runs.length > 0 && <span className="text-muted-foreground/60">· {hint}</span>}
    </div>
  );
}
