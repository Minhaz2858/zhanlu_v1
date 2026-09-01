import { useState, useEffect } from 'react';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Loader2, Copy } from 'lucide-react';
import ProjectSelector from '@/components/automation/ProjectSelector';
import { useProjectSync } from '@/lib/useProjectSync';

const inputCls = 'flex w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-sm ring-offset-background focus:outline-none focus:ring-1 focus:ring-ring';

/**
 * DuplicateAgentDialog — clones an existing agent under a new (or same)
 * name and project.
 *
 * Project picker writes BOTH `project_id` (FK) and `project` (legacy
 * name) so the agent appears in the new Project Detail page immediately.
 */
export default function DuplicateAgentDialog({ open, onOpenChange, agent, onDone }) {
  const { t, lang } = useLanguage();
  const { resolve } = useProjectSync();

  const [name, setName] = useState('');
  // Selection held as the raw picker value (id, name, or 'global').
  const [selection, setSelection] = useState('global');
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (open && agent) {
      const suffix = lang === 'en' ? ' Copy' : ' 副本';
      setName((agent.name || t.agentConfig.title) + suffix);
      // Prefer project_id when present, fall back to legacy name string.
      setSelection(agent.project_id || agent.project || 'global');
    }
  }, [open, agent, lang, t.agentConfig.title]);

  async function handleDuplicate() {
    if (!name.trim()) return;
    setLoading(true);
    try {
      const { id, created_date, updated_date, created_by_id, ...rest } = agent;
      const { project_id, project } = resolve(selection);
      const created = await base44.entities.AgentApp.create({
        ...rest,
        name: name.trim(),
        project_id: project_id || undefined,
        project: project || 'global',
      });
      onDone?.(created.id);
      onOpenChange(false);
    } catch { /* noop */ }
    finally { setLoading(false); }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><Copy className="h-4 w-4" /> {t.agentConfig.duplicate}</DialogTitle>
          <DialogDescription>{t.agentConfig.duplicateDesc}</DialogDescription>
        </DialogHeader>
        <div className="space-y-4 py-2">
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-foreground">{t.agentConfig.duplicateName}</label>
            <input value={name} onChange={(e) => setName(e.target.value)} className={inputCls} autoFocus onKeyDown={(e) => { if (e.key === 'Enter') handleDuplicate(); }} />
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-foreground">{t.agentConfig.duplicateProject}</label>
            <ProjectSelector value={selection} onChange={setSelection} />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>{t.kb.cancel}</Button>
          <Button onClick={handleDuplicate} disabled={loading || !name.trim()} className="gap-1.5">
            {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Copy className="h-3.5 w-3.5" />}
            {loading ? t.agentConfig.duplicating : t.agentConfig.duplicate}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
