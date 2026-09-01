/**
 * AddAgentToProjectDialog — modal for adding existing AgentApps to the
 * current project, and removing them (i.e. removing their membership row
 * from the `project_agents` association table).
 *
 * Pulls a sizable window of agents and shows a checkbox per row. Already-
 * in-project agents (members of project_agents) are checked by default;
 * un-checking them removes their membership. Un-bound agents can be
 * checked to add a new membership.
 *
 * Many-to-many: an agent checked in another project's dialog can be
 * checked here too — the same agent can belong to multiple projects
 * simultaneously. We just create one membership row per (project, agent)
 * pair.
 *
 * This addresses the "agents show all bound" complaint by giving the
 * user explicit control over each project's membership.
 */
import { useState, useEffect } from 'react';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { filterUserAgents } from '@/lib/systemAgents';
import { Bot, Check, Loader2, Search } from 'lucide-react';

export default function AddAgentToProjectDialog({
  open,
  onOpenChange,
  project,
  excludeIds = [],
  onAdded,
  onRemoved,
}) {
  const { t, lang } = useLanguage();
  const isEn = lang === 'en';

  const [agents, setAgents] = useState([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  // `checked` = the set of agent ids that will be in this project after
  // the dialog is saved. Initialised to boundHere so the user's first
  // action is "uncheck what I don't want"; binding a new agent means
  // checking an un-bound row.
  const [checked, setChecked] = useState(() => new Set());
  const [search, setSearch] = useState('');

  // Reset to current bound state whenever the dialog opens. We translate
  // `excludeIds` into the initial `checked` set.
  useEffect(() => {
    if (open) {
      setChecked(new Set(excludeIds));
      setSearch('');
      load();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  async function load() {
    setLoading(true);
    try {
      const all = await base44.entities.AgentApp.list('-updated_date', 500);
      // Don't surface platform system agents in the "add to project"
      // picker. They are seeded and managed by the runtime, not by
      // users — adding general_assistant to a project would be a
      // no-op anyway (the runtime already auto-resolves it for any
      // chat that has no user-picked agent). Filtering here keeps
      // the picker focused on user-created agents.
      setAgents(filterUserAgents(all));
    } catch {
      setAgents([]);
    } finally {
      setLoading(false);
    }
  }

  const boundHere = new Set(excludeIds);

  const filtered = agents.filter((a) => {
    const q = search.trim().toLowerCase();
    if (!q) return true;
    return a.name && a.name.toLowerCase().includes(q);
  });

  function toggle(id) {
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  // Diff: agents we *will add* (checked but not currently bound) and
  // agents we *will remove* (currently bound but un-checked).
  const addIds = Array.from(checked).filter((id) => !boundHere.has(id));
  const removeIds = Array.from(boundHere).filter((id) => !checked.has(id));

  async function save() {
    if (addIds.length === 0 && removeIds.length === 0) {
      onOpenChange(false);
      return;
    }
    setSaving(true);
    try {
      // Detect if ProjectAgent is registered on the backend. If not,
      // we fall back to the legacy `AgentApp.update({ project_id })`
      // approach so this dialog still works on older backend
      // deployments. We probe once via .filter() and cache the result.
      let projectAgentAvailable = false;
      try {
        await base44.entities.ProjectAgent.filter({ project_id: project.id }, '', 1);
        projectAgentAvailable = true;
      } catch (e) {
        console.warn('ProjectAgent not available on backend, using legacy fallback:', e);
        projectAgentAvailable = false;
      }

      // ADD: create a ProjectAgent membership row per added agent (new
      // backend), OR set the agent's project_id to this project (legacy
      // backend).
      for (const id of addIds) {
        if (projectAgentAvailable) {
          try {
            await base44.entities.ProjectAgent.create({
              project_id: project.id,
              agent_id: id,
              role: 'member',
            });
          } catch (e) {
            // If a membership row already exists (unique constraint), ignore.
            console.warn('ProjectAgent.create failed (may already exist):', e);
          }
        } else {
          // Legacy fallback: stamp the agent's primary project_id so
          // the project's AgentApp.project_id == current filter picks
          // it up.
          try {
            await base44.entities.AgentApp.update(id, {
              project_id: project.id,
              project: project.name,
            });
          } catch (e) {
            console.warn('AgentApp.update (legacy add) failed:', e);
          }
        }
      }
      // REMOVE: delete the ProjectAgent membership row(s) (new
      // backend), OR clear the agent's project_id (legacy backend).
      // We do NOT touch the agent's primary project_id on the new
      // backend — that's the legacy "home project" pointer and may
      // legitimately point at another project the agent still belongs
      // to.
      if (removeIds.length > 0) {
        if (projectAgentAvailable) {
          const memberships = await base44.entities.ProjectAgent
            .filter({ project_id: project.id })
            .catch(() => []);
          const byAgentId = new Map(
            (Array.isArray(memberships) ? memberships : []).map((m) => [m.agent_id, m])
          );
          for (const id of removeIds) {
            const m = byAgentId.get(id);
            if (m && m.id) {
              try { await base44.entities.ProjectAgent.delete(m.id); }
              catch (e) { console.warn('ProjectAgent.delete failed:', e); }
            } else {
              // No membership row found — use legacy fallback ONLY if
              // the agent has no other memberships. Otherwise leave
              // the primary project_id alone.
              const allMine = await base44.entities.ProjectAgent
                .filter({ agent_id: id })
                .catch(() => []);
              if (!Array.isArray(allMine) || allMine.length === 0) {
                try {
                  await base44.entities.AgentApp.update(id, {
                    project_id: null,
                    project: 'global',
                  });
                } catch { /* noop */ }
              }
            }
          }
        } else {
          // Legacy backend — clear the agent's project_id (only if it
          // currently points at THIS project, otherwise we'd unbind
          // it from another project).
          for (const id of removeIds) {
            try {
              const agent = await base44.entities.AgentApp.get(id).catch(() => null);
              if (agent && agent.project_id === project.id) {
                await base44.entities.AgentApp.update(id, {
                  project_id: null,
                  project: 'global',
                });
              }
            } catch (e) {
              console.warn('AgentApp.update (legacy remove) failed:', e);
            }
          }
        }
      }
      onAdded?.(addIds);
      onRemoved?.(removeIds);
      onOpenChange(false);
    } catch (e) {
      console.error('Agent membership change failed', e);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Bot className="h-5 w-5 text-primary" />
            {isEn ? 'Manage Agents' : '管理项目 Agent'}
          </DialogTitle>
          <DialogDescription>
            {isEn
              ? <>Tick agents to <b>add</b> to <span className="font-medium">{project.name}</span>; un-tick to remove. An agent can be a member of multiple projects.</>
              : <>勾选即<b>添加</b>到项目 <span className="font-medium">{project.name}</span>，取消勾选即移除。一个 Agent 可以同时属于多个项目。</>}
          </DialogDescription>
        </DialogHeader>

        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={isEn ? 'Search by name…' : '搜索名称…'}
            className="w-full rounded-md border border-border bg-background py-1.5 pl-8 pr-3 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
          />
        </div>

        <div className="max-h-72 overflow-y-auto rounded-lg border border-border">
          {loading ? (
            <div className="flex justify-center py-10"><Loader2 className="h-5 w-5 animate-spin text-muted-foreground" /></div>
          ) : filtered.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-10 text-xs text-muted-foreground">
              <Bot className="mb-2 h-6 w-6" />
              {isEn ? 'No agents available.' : '暂无可用的 Agent。'}
            </div>
          ) : (
            <ul className="divide-y divide-border">
              {filtered.map((a) => {
                const isChecked = checked.has(a.id);
                const isBound = boundHere.has(a.id);
                return (
                  <li key={a.id}>
                    <label
                      className={`flex cursor-pointer items-start gap-3 px-3 py-2.5 transition-colors hover:bg-secondary/60 ${
                        isChecked ? 'bg-primary/5' : ''
                      }`}
                    >
                      <span className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border ${
                        isChecked ? 'border-primary bg-primary text-primary-foreground' : 'border-border bg-background'
                      }`}>
                        {isChecked && <Check className="h-3 w-3" />}
                      </span>
                      <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
                        <Bot className="h-3.5 w-3.5" />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm font-medium">{a.name}</span>
                        <span className="block truncate text-[11px] text-muted-foreground">
                          {a.description || (isEn ? 'No description' : '暂无描述')}
                        </span>
                        {isBound && isChecked && (
                          <span className="mt-0.5 inline-flex items-center gap-1 rounded bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">
                            {isEn ? 'in this project' : '已在项目中'}
                          </span>
                        )}
                      </span>
                      <input
                        type="checkbox"
                        checked={isChecked}
                        onChange={() => toggle(a.id)}
                        className="sr-only"
                      />
                    </label>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        <DialogFooter>
          <span className="mr-auto text-xs text-muted-foreground">
            {addIds.length > 0 || removeIds.length > 0 ? (
              <>
                {addIds.length > 0 && (
                  <span className="mr-2 rounded bg-emerald-100 px-1.5 py-0.5 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300">
                    +{addIds.length} {isEn ? 'add' : '添加'}
                  </span>
                )}
                {removeIds.length > 0 && (
                  <span className="rounded bg-red-100 px-1.5 py-0.5 text-red-700 dark:bg-red-900/30 dark:text-red-300">
                    -{removeIds.length} {isEn ? 'remove' : '移除'}
                  </span>
                )}
              </>
            ) : (isEn ? 'No changes' : '无变更')}
          </span>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={saving}>
            {isEn ? 'Cancel' : '取消'}
          </Button>
          <Button onClick={save} disabled={saving} className="gap-1.5">
            {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
            {isEn ? 'Save' : '保存'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
