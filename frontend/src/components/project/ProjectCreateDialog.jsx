import { useState, useEffect } from 'react';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
  DialogDescription, DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { base44 } from '@/api/base44Client';
import { Loader2, Folder } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';

// Color presets were removed: the user asked for a simple, color-free
// project list. The ``color`` field on the Project model is still
// present in the backend (for backward compatibility) but the create
// dialog no longer surfaces a picker or sends ``color`` to the API.

/**
 * ProjectCreateDialog — modal for creating a Project.
 *
 * Inputs:
 *   - name (required)
 *   - description (optional)
 *   - color (one of 8 preset swatches + free input via <input type="color">)
 *
 * Calls `base44.entities.Project.create()` and returns the created record to
 * `onCreated` so the caller can optimistically append it to its list.
 */
export default function ProjectCreateDialog({ open, onOpenChange, onCreated }) {
  const { t, lang } = useLanguage();
  const isEn = lang === 'en';
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  // Reset form whenever the dialog re-opens.
  useEffect(() => {
    if (open) {
      setName('');
      setDescription('');
      setError(null);
    }
  }, [open]);

  async function create() {
    setError(null);
    if (!name.trim()) {
      setError(t.projectDetail?.nameRequired || (isEn ? 'Name is required' : '名称不能为空'));
      return;
    }
    setSaving(true);
    try {
      const created = await base44.entities.Project.create({
        name: name.trim(),
        description: description.trim() || undefined,
        status: 'active',
      });
      onCreated?.(created);
      onOpenChange(false);
    } catch (err) {
      console.error('Project create failed:', err);
      setError(isEn ? 'Failed to create project' : '创建项目失败');
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <span
              className="inline-flex h-7 w-7 items-center justify-center rounded-lg bg-secondary text-muted-foreground"
            >
              <Folder className="h-4 w-4" />
            </span>
            {isEn ? 'New Project' : '新建项目'}
          </DialogTitle>
          <DialogDescription>
            {isEn
              ? 'Group agents, data sources, files, chats and automations into one container.'
              : '将 Agent、数据源、文件、对话与自动化任务归类到同一项目下。'}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div>
            <Label className="mb-1.5 block text-xs">{isEn ? 'Name' : '名称'}</Label>
            <Input
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={isEn ? 'e.g. Sales Analytics' : '例如：销售分析'}
              onKeyDown={(e) => { if (e.key === 'Enter') create(); }}
            />
          </div>

          <div>
            <Label className="mb-1.5 block text-xs">{isEn ? 'Description' : '描述'}</Label>
            <Textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={isEn
                ? 'What is this project about? What outcomes are you aiming for?'
                : '这个项目主要解决什么？希望达到什么结果？'}
              rows={3}
              className="resize-none"
            />
          </div>

          {/* Color picker removed: the user asked for a simple,
              color-free project list. The ``color`` field on the
              Project model is still present in the backend (for
              backward compatibility) but the create dialog no longer
              surfaces a picker or sends ``color`` to the API. */}

          {error && (
            <p className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-xs text-red-600 dark:bg-red-900/20 dark:text-red-300">
              {error}
            </p>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={saving}>
            {t.common?.cancel || (isEn ? 'Cancel' : '取消')}
          </Button>
          <Button onClick={create} disabled={saving} className="gap-1.5">
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Folder className="h-4 w-4" />}
            {isEn ? 'Create Project' : '创建项目'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
