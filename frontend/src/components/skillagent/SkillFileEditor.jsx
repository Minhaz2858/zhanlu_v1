import { useEffect, useRef, useState } from 'react';
import { X, Save, Loader2, Check, FileText } from 'lucide-react';
import { updateSkillDraftFile } from '@/api/skillStudio';

/**
 * Inline markdown editor for a single file inside an active SkillDraft.
 * Opened from the SkillDraftPanel when the user clicks a .md node. Writes
 * changes back to the backend draft store so the orchestrator and the live
 * folder tree both see the edited content.
 */
export default function SkillFileEditor({ path, content, conversationId, onClose, onSaved }) {
  const [value, setValue] = useState(content || '');
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState('');
  const textareaRef = useRef(null);

  useEffect(() => {
    setValue(content || '');
    setSaved(false);
    setError('');
  }, [path, content]);

  useEffect(() => {
    textareaRef.current?.focus();
  }, []);

  async function handleSave() {
    if (!conversationId || saving) return;
    setSaving(true);
    setError('');
    try {
      const updatedDraft = await updateSkillDraftFile(conversationId, path, value);
      setSaved(true);
      onSaved?.(updatedDraft);
      setTimeout(() => setSaved(false), 1600);
    } catch (e) {
      setError(e.message || 'Failed to save file');
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" onClick={onClose} />
      <div className="relative flex h-[85vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-white/20 bg-white shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-border bg-white/80 px-5 py-3 backdrop-blur-md">
          <div className="flex items-center gap-2.5 min-w-0">
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-indigo-500 to-purple-500 text-white">
              <FileText className="h-4 w-4" />
            </span>
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold text-foreground">{path}</p>
              <p className="text-[11px] text-muted-foreground">Markdown file</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="flex h-8 w-8 items-center justify-center rounded-lg text-muted-foreground transition hover:bg-secondary hover:text-foreground"
            aria-label="Close editor"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Editor body */}
        <div className="flex-1 overflow-hidden bg-[#F9FAFB]">
          <textarea
            ref={textareaRef}
            value={value}
            onChange={(e) => { setValue(e.target.value); setSaved(false); }}
            spellCheck={false}
            className="h-full w-full resize-none bg-transparent p-5 font-mono text-[13px] leading-relaxed text-foreground focus:outline-none"
          />
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between border-t border-border bg-white/80 px-5 py-3 backdrop-blur-md">
          <p className={`text-xs ${error ? 'text-red-500' : 'text-muted-foreground'}`}>
            {error || (saved ? 'Saved to draft' : 'Edits are saved back to the active draft')}
          </p>
          <div className="flex items-center gap-2">
            <button
              onClick={onClose}
              className="rounded-lg border border-border px-3 py-1.5 text-xs font-medium text-foreground transition hover:bg-secondary"
            >
              Cancel
            </button>
            <button
              onClick={handleSave}
              disabled={saving}
              className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-xs font-medium text-primary-foreground transition disabled:opacity-50"
            >
              {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : saved ? <Check className="h-3.5 w-3.5" /> : <Save className="h-3.5 w-3.5" />}
              {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
