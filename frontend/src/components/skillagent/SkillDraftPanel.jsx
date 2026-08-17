import { useMemo, useState } from 'react';
import {
  Folder, FolderOpen, FileText, FileCode, ChevronRight, Sparkles,
  Check, Cloud, Loader2, Trash2, X, Boxes, FileArchive,
} from 'lucide-react';
import SkillFileEditor from './SkillFileEditor';
import { discardSkillDraft } from '@/api/skillStudio';

const STATUS_LABELS = {
  collecting: 'Understanding…',
  proposing: 'Proposing layout…',
  drafting: 'Drafting…',
  review: 'Ready to review',
  ready: 'Ready to save',
  saved: 'Saved',
};

const STATUS_ACTIVE = new Set(['collecting', 'proposing', 'drafting']);

/**
 * Live folder tree shown while the Skill Agent is creating a skill.
 * Renders SKILL.md + references/*.md + assets/templates/* from the active
 * SkillDraft payload, with per-file status badges. Clicking a .md file opens
 * the inline markdown editor.
 */
export default function SkillDraftPanel({ draft, conversationId, onClose, onDiscarded }) {
  const [openFolders, setOpenFolders] = useState({ references: true, assets: true, templates: true });
  const [editingPath, setEditingPath] = useState(null);
  const [discarding, setDiscarding] = useState(false);

  const status = draft?.status || 'collecting';
  const isActive = STATUS_ACTIVE.has(status);

  const referenceFiles = useMemo(() => {
    const refs = draft?.references || {};
    return Object.keys(refs).sort();
  }, [draft]);

  const assetFiles = useMemo(() => {
    const assets = draft?.assets || {};
    return Object.keys(assets).sort();
  }, [draft]);

  const hasSkillMd = !!(draft?.skill_md && draft.skill_md.trim());
  const totalFiles = (hasSkillMd ? 1 : 0) + referenceFiles.length + assetFiles.length;

  function toggleFolder(key) {
    setOpenFolders((prev) => ({ ...prev, [key]: !prev[key] }));
  }

  async function handleDiscard() {
    if (!conversationId || discarding) return;
    setDiscarding(true);
    try {
      await discardSkillDraft(conversationId);
      onDiscarded?.();
    } catch (e) {
      console.error('Failed to discard draft:', e);
    } finally {
      setDiscarding(false);
    }
  }

  function openEditor(path, content) {
    setEditingPath({ path, content });
  }

  const renderStatusBadge = (kind) => {
    if (status === 'saved') {
      return <Cloud className="h-3.5 w-3.5 text-blue-500" />;
    }
    if (kind === 'done') {
      return <Check className="h-3.5 w-3.5 text-green-500" />;
    }
    if (isActive) {
      return <Loader2 className="h-3.5 w-3.5 animate-spin text-amber-500" />;
    }
    return <Check className="h-3.5 w-3.5 text-green-500" />;
  };

  const FolderRow = ({ label, icon: Icon, count, openKey, children }) => {
    const open = openFolders[openKey];
    return (
      <div>
        <button
          onClick={() => toggleFolder(openKey)}
          className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm text-foreground transition hover:bg-secondary/60"
        >
          <ChevronRight className={`h-3.5 w-3.5 text-muted-foreground transition-transform ${open ? 'rotate-90' : ''}`} />
          {open ? <FolderOpen className="h-4 w-4 text-indigo-500" /> : <Folder className="h-4 w-4 text-indigo-500" />}
          <span className="font-medium">{label}</span>
          <span className="ml-auto rounded-full bg-secondary px-2 py-0.5 text-[10px] font-medium text-muted-foreground">{count}</span>
        </button>
        <div className={`overflow-hidden transition-all duration-200 ${open ? 'max-h-96' : 'max-h-0'}`}>
          <div className="ml-4 border-l border-border pl-2">{children}</div>
        </div>
      </div>
    );
  };

  const FileRow = ({ icon: Icon, name, kind = 'done', onClick, editable = false }) => (
    <button
      onClick={onClick}
      disabled={!onClick}
      className={`flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-[13px] transition ${onClick ? 'hover:bg-secondary/60' : 'cursor-default'}`}
    >
      <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      <span className="truncate text-foreground">{name}</span>
      <span className="ml-auto flex items-center gap-1">
        {renderStatusBadge(kind)}
        {editable && onClick && <span className="text-[10px] text-muted-foreground">edit</span>}
      </span>
    </button>
  );

  return (
    <div className="flex h-full flex-col overflow-hidden bg-white/70 backdrop-blur-md">
      {/* Header */}
      <div className="border-b border-white/20 bg-gradient-to-r from-indigo-500/10 to-purple-500/10 px-4 py-3">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-indigo-500" />
              <span className="truncate bg-gradient-to-r from-indigo-500 to-purple-500 bg-clip-text font-display text-base font-semibold text-transparent">
                {draft?.name || 'New Skill'}
              </span>
            </div>
            <div className="mt-1 flex items-center gap-1.5">
              <span className="relative flex h-2 w-2">
                {isActive && <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-400 opacity-75" />}
                <span className={`relative inline-flex h-2 w-2 rounded-full ${status === 'saved' ? 'bg-blue-500' : isActive ? 'bg-amber-400' : 'bg-green-500'}`} />
              </span>
              <span className="text-[11px] font-medium text-muted-foreground">{STATUS_LABELS[status] || status}</span>
              <span className="text-[11px] text-muted-foreground">· {totalFiles} file{totalFiles === 1 ? '' : 's'}</span>
            </div>
          </div>
          <button
            onClick={onClose}
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted-foreground transition hover:bg-secondary hover:text-foreground"
            aria-label="Close panel"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        {status === 'ready' && (
          <p className="mt-2 rounded-lg bg-green-500/10 px-2.5 py-1.5 text-[11px] text-green-700">
            Reply &quot;save&quot; or &quot;confirm&quot; in chat to persist this skill.
          </p>
        )}
      </div>

      {/* Folder tree */}
      <div className="flex-1 space-y-1 overflow-y-auto px-2 py-3">
        <div className="mb-1 px-2 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Skill package</div>

        {/* SKILL.md */}
        <FileRow
          icon={FileText}
          name="SKILL.md"
          kind={hasSkillMd ? 'done' : 'drafting'}
          onClick={hasSkillMd ? () => openEditor('SKILL.md', draft.skill_md) : null}
          editable
        />

        {/* references/ */}
        <FolderRow label="references" icon={Folder} count={referenceFiles.length} openKey="references">
          {referenceFiles.length === 0 ? (
            <p className="px-2 py-1 text-[11px] text-muted-foreground">No reference files yet</p>
          ) : (
            referenceFiles.map((fn) => (
              <FileRow
                key={fn}
                icon={FileCode}
                name={fn}
                kind="done"
                onClick={() => openEditor(`references/${fn}`, (draft.references || {})[fn])}
                editable
              />
            ))
          )}
        </FolderRow>

        {/* assets/templates/ */}
        <FolderRow label="assets" icon={Folder} count={assetFiles.length} openKey="assets">
          <FolderRow label="templates" icon={Boxes} count={assetFiles.length} openKey="templates">
            {assetFiles.length === 0 ? (
              <p className="px-2 py-1 text-[11px] text-muted-foreground">No templates uploaded</p>
            ) : (
              assetFiles.map((rel) => (
                <FileRow key={rel} icon={FileArchive} name={rel.replace(/^templates\//, '')} kind="done" />
              ))
            )}
          </FolderRow>
        </FolderRow>
      </div>

      {/* Footer actions */}
      <div className="border-t border-border px-3 py-2.5">
        <button
          onClick={handleDiscard}
          disabled={discarding}
          className="inline-flex w-full items-center justify-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground transition hover:border-red-200 hover:bg-red-50 hover:text-red-600 disabled:opacity-50"
        >
          {discarding ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
          Discard draft
        </button>
      </div>

      {/* Inline editor */}
      {editingPath && (
        <SkillFileEditor
          path={editingPath.path}
          content={editingPath.content}
          conversationId={conversationId}
          onClose={() => setEditingPath(null)}
          onSaved={(updated) => {
            // Keep the editor open; parent will refresh draft via subscription.
            if (updated) console.log('Draft file saved:', editingPath.path);
          }}
        />
      )}
    </div>
  );
}
