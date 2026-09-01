import { useState, useEffect, useRef } from 'react';
import { Send, Square, X, Wrench, Bot, Folder, FileText, FileSpreadsheet, Image as ImageIcon, File, Database } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import { isSystemAgent } from '@/lib/systemAgents';
import { isUngroupedProjectName } from '@/lib/projectGrouping';
import { base44 } from '@/api/base44Client';
import PlusMenu from './PlusMenu';
import VoiceInput from './VoiceInput';
import AgentSuggestions from './AgentSuggestions';
import FilePreviewModal from './FilePreviewModal';
import EffectiveModelBadge from './EffectiveModelBadge';
import SkillPreviewPopover from './SkillPreviewPopover';

// Pick an icon for an attachment chip based on its extension. Falls back
// to a generic File icon. Used by the chip-strip below so each queued
// attachment shows its kind (PDF, sheet, image, …) at a glance.
function attachmentIcon(name) {
  const ext = (name || '').split('.').pop().toLowerCase();
  if (['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp', 'tiff', 'tif'].includes(ext)) return ImageIcon;
  if (['xlsx', 'xls', 'csv', 'tsv'].includes(ext)) return FileSpreadsheet;
  if (['pdf', 'docx', 'pptx', 'ppt', 'txt', 'md', 'json', 'html', 'htm'].includes(ext)) return FileText;
  return File;
}

// Allowed extensions — must match the backend UploadFile allowlist so a
// dropped/pasted file is never rejected server-side after the user
// already sees it queued. Used by the drag-drop and paste handlers.
const ALLOWED_EXTS = new Set([
  'txt','md','csv','json','html','htm','pdf','docx','pptx','ppt','xlsx','xls',
  'png','jpg','jpeg','webp','gif','bmp','tiff','tif',
  'mp3','m4a','wav','mp4','mov','webm',
]);

function isAllowedFile(file) {
  if (!file) return false;
  // Images pasted from clipboard often have no name — accept by type.
  if (file.type && file.type.startsWith('image/')) return true;
  const ext = (file.name || '').split('.').pop().toLowerCase();
  return ALLOWED_EXTS.has(ext);
}

/**
 * ChatInput — message input with chips for active skill / agent / attachments.
 *
 * The "DB: …" database chip and the "Read from my database" quick-action
 * button were intentionally removed — those affordances are still
 * available on the agent's Data Sources page (My Space → Agents → Data
 * Sources), and the input itself stays focused on the conversation.
 * Tooling for live data reads is exposed by the agent's tools at
 * runtime, not as a one-click chip in the input.
 *
 * The active agent's bound database Kbs are still kept on
 * `activeAgent.knowledge_bases` and consumed by the data-source
 * runtime, so removing the chip does NOT remove the agent's ability
 * to read the database — it just hides the visual hint.
 */
export default function ChatInput({
  value, onChange, onSend, onSelectSkill, onSelectAgent, onSelectProject,
  onRemoveSkill, onRemoveAgent, activeSkill, activeAgent,
  disabled, centered, inputRef, onUploadFile,
  attachments = [], onRemoveAttachment,
  isStreaming, onStop,
  // Automation-specific stop path. Set by Chat.jsx when the in-flight
  // "response" is actually a background automation run rather than an
  // SSE chat stream. When provided AND ``isStreaming`` is true, the
  // Stop button calls this instead of ``onStop``. The parent is
  // expected to fire ``POST /api/automations/executions/{id}/cancel``
  // and the UI state (loading / latestExecution) will refresh via the
  // existing by-session poll.
  onStopAutomation,
  pendingProject, onClearProject,
  inheritedKbCount = 0,
  pendingProjectId = null,
}) {
  const { t, lang } = useLanguage();
  const [dragCounter, setDragCounter] = useState(0);
  const [rejectedFile, setRejectedFile] = useState(null);
  const [previewIdx, setPreviewIdx] = useState(null);
  // Track an in-flight cancel click so the Stop button briefly shows
  // "Cancelling…" instead of re-firing the cancel. The actual state
  // transition (running → cancelled) lands in the by-session poll a
  // tick later.
  const [cancelInFlight, setCancelInFlight] = useState(false);
  const stopHandler = onStopAutomation || onStop;
  const stopTitleKey = onStopAutomation
    ? (lang === 'en' ? 'Stop run' : '停止运行')
    : (lang === 'en' ? 'Stop generating' : '停止生成');

  // Whether file upload is allowed for this user. Read from the per-user
  // UserSetting row (file_upload_enabled, default true). The backend
  // enforces the same setting with a 403 — this state only hides the
  // affordances so the UI matches the policy. Defaults to true while the
  // fetch is in flight so the toolbar never flashes empty.
  const [uploadEnabled, setUploadEnabled] = useState(true);

  useEffect(() => {
    let alive = true;
    base44.entities.UserSetting.list('', 1)
      .then((rows) => {
        if (!alive) return;
        setUploadEnabled(!(rows?.[0]?.file_upload_enabled === false));
      })
      .catch(() => { /* keep default (enabled) */ });
    return () => { alive = false; };
  }, []);

  function handleSend() {
    if (!value.trim() || disabled) return;
    onSend(value.trim());
  }

  // Phase 3: drag-and-drop onto the composer. Each dropped file is
  // validated against the backend allowlist and uploaded via the same
  // onUploadFile callback the PlusMenu uses (so attachment state, UserFile
  // rows, and chip rendering are identical). Disallowed files trigger a
  // transient "not supported" hint so the user knows why nothing happened.
  async function handleDrop(e) {
    e.preventDefault();
    e.stopPropagation();
    setDragCounter(0);
    if (disabled) return;
    const files = Array.from(e.dataTransfer?.files || []);
    if (!files.length) return;
    if (!uploadEnabled) {
      setRejectedFile(lang === 'en' ? 'File upload is disabled in Settings' : '文件上传已禁用（请在设置中开启）');
      setTimeout(() => setRejectedFile(null), 3000);
      return;
    }
    const ok = files.filter(isAllowedFile);
    const bad = files.filter((f) => !isAllowedFile(f));
    if (bad.length) {
      const names = bad.map((f) => f.name || 'file').join(', ');
      setRejectedFile(names);
      setTimeout(() => setRejectedFile(null), 3000);
    }
    // Upload in parallel so the UI doesn't freeze on multi-file drops
    await Promise.all(ok.map((file) => onUploadFile?.(file).catch(() => {})));
  }

  // Phase 3: paste images from clipboard. A paste can carry both text
  // (forwarded to the textarea as normal) and image blobs (uploaded as
  // attachments). Synthetic filenames are generated since clipboard items
  // have no name. Only the first image is taken per paste to avoid
  // spamming the attachments on an over-eager paste.
  async function handlePaste(e) {
    if (disabled || !uploadEnabled) return;
    const items = Array.from(e.clipboardData?.items || []);
    let tookImage = false;
    for (const item of items) {
      if (item.kind === 'file' && item.type.startsWith('image/')) {
        const file = item.getAsFile();
        if (!file) continue;
        const ext = (file.type.split('/')[1] || 'png').split(';')[0];
        const named = new File([file], `pasted-image.${ext}`, { type: file.type });
        try {
          await onUploadFile?.(named);
          tookImage = true;
        } catch { /* ignore — parent handles errors */ }
        break; // one image per paste
      }
    }
    // If we took an image, swallow the default paste so the textarea
    // doesn't also receive a filename string.
    if (tookImage) e.preventDefault();
  }

  return (
    <div
      className={centered ? '' : 'border-t border-border bg-card/50 px-6 py-4'}
      onDragEnter={(e) => { e.preventDefault(); if (!disabled) setDragCounter((c) => c + 1); }}
      onDragOver={(e) => { e.preventDefault(); }}
      onDragLeave={(e) => { e.preventDefault(); setDragCounter((c) => Math.max(0, c - 1)); }}
      onDrop={handleDrop}
    >
      <div className={centered ? '' : 'mx-auto max-w-5xl'}>
        <div className="relative rounded-xl border border-border bg-card shadow-sm transition focus-within:ring-2 focus-within:ring-primary/30">
          {/* Phase 3: drag-over overlay — shown while the user drags files
              over the composer. Rendered absolutely inside the input card
              so it never pushes the textarea layout. */}
          {dragCounter > 0 && !disabled && (
            <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-xl border-2 border-dashed border-primary/60 bg-primary/5 backdrop-blur-[1px]">
              <div className="flex flex-col items-center gap-1.5 text-primary">
                <FileText className="h-6 w-6" />
                <span className="text-xs font-medium">
                  {lang === 'en' ? 'Drop files to attach' : '拖放文件以上传'}
                </span>
              </div>
            </div>
          )}
          {/* Phase 3: rejected-file hint (auto-dismissed after 3s). */}
          {rejectedFile && (
            <div className="absolute -top-9 left-0 z-10 rounded-md bg-destructive px-2.5 py-1 text-xs text-destructive-foreground shadow-sm">
              {lang === 'en' ? `"${rejectedFile}" not supported` : `"${rejectedFile}" 不受支持`}
            </div>
          )}
          {!value.trim() && <AgentSuggestions agent={activeAgent} lang={lang} onSelect={onChange} />}
          <textarea
            ref={inputRef}
            data-chat-input="true"
            data-testid="chat-textarea"
            value={value}
            onChange={(e) => onChange(e.target.value)}
            onPaste={handlePaste}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); }
            }}
            placeholder={t.chat.placeholder}
            rows={2}
            disabled={disabled}
            className="w-full resize-none bg-transparent px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none"
          />
          <div className="flex items-center justify-between gap-2 px-3 pb-2">
            {/*
              Context chips live BESIDE the + button so they're
              immediately discoverable — hierarchy reads
              "project → agent → skills → attachments".
            */}
            <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5">
              <PlusMenu
                onUpload={onUploadFile}
                onSelectSkill={onSelectSkill}
                onSelectAgent={onSelectAgent}
                onSelectProject={onSelectProject}
                activeSkill={activeSkill}
                activeAgent={activeAgent}
                activeProject={pendingProject}
                disabled={disabled}
                uploadEnabled={uploadEnabled}
              />
              {/* The project chip is only shown for a real, bound
                  project. A session without a project is the
                  implicit "Ungrouped" default state — not a
                  selected state with a removable tag — so we
                  hide the chip entirely in that case. The X
                  button is removed because there's nothing to
                  clear.

                  ``isUngroupedProjectName`` is a defense-in-depth
                  check: even if a legacy ChatSession row has
                  ``project = "Ungrouped"`` from before the data
                  normalization, the chip will not render. */}
              {pendingProject && !isUngroupedProjectName(pendingProject) && (
                <span
                  title={lang === 'en' ? `New chats will be created in "${pendingProject}"` : `新聊天将创建于"${pendingProject}"`}
                  className="inline-flex max-w-[14rem] items-center gap-1.5 rounded-full border border-amber-500/50 bg-amber-500/15 px-2.5 py-1 text-xs font-medium text-amber-700 dark:text-amber-300"
                >
                  <Folder className="h-3 w-3 shrink-0" />
                  <span className="truncate">{pendingProject}</span>
                  {/* Phase 4: visible "inheriting N data sources" badge
                      — surfaces the project-context binding that
                      data_source_runtime._extend_with_project_kbs applies
                      to *every* agent in the project, not just the
                      project-specific one. Without this, users only
                      learn about the inheritance by asking the agent
                      "what can you do?" and getting an answer that
                      mentions resources they thought belonged only to
                      the project agent. */}
                  {inheritedKbCount > 0 && (
                    <span
                      title={
                        lang === 'en'
                          ? `Inheriting ${inheritedKbCount} data source${inheritedKbCount === 1 ? '' : 's'} from "${pendingProject}"`
                          : `正在从"${pendingProject}"继承 ${inheritedKbCount} 个数据源`
                      }
                      data-testid="inherited-kb-badge"
                      className="inline-flex items-center gap-1 rounded-full border border-amber-700/40 bg-amber-700/10 px-1.5 py-0.5 text-[10px] font-semibold tabular-nums"
                    >
                      <Database className="h-2.5 w-2.5" />
                      {inheritedKbCount}
                    </span>
                  )}
                  {onClearProject && (
                    <button
                      type="button"
                      onClick={() => onClearProject()}
                      className="shrink-0 rounded-full p-0.5 transition-colors hover:bg-amber-500/20"
                      aria-label={lang === 'en' ? `Clear project (return to default)` : `清除项目（返回默认）`}
                    >
                      <X className="h-3 w-3" />
                    </button>
                  )}
                </span>
              )}
              {activeAgent && !isSystemAgent(activeAgent) && (
                <span className="inline-flex max-w-[14rem] items-center gap-1.5 rounded-full border border-primary/40 bg-primary/10 px-2.5 py-1 text-xs font-medium text-primary">
                  <Bot className="h-3 w-3 shrink-0" />
                  <span className="truncate">{activeAgent.name}</span>
                  <button
                    type="button"
                    onClick={onRemoveAgent}
                    className="shrink-0 transition-colors hover:text-primary/70"
                    aria-label={lang === 'en' ? 'Remove agent' : '移除 Agent'}
                  >
                    <X className="h-3 w-3" />
                  </button>
                </span>
              )}
              <EffectiveModelBadge
                projectId={pendingProjectId}
                agentName={activeAgent?.name}
              />
              {activeSkill && (
                <SkillPreviewPopover skill={activeSkill} onRemove={onRemoveSkill} />
              )}
              {attachments.map((a, i) => {
                const Icon = attachmentIcon(a.name);
                return (
                  <button
                    key={`att-${i}`}
                    type="button"
                    onClick={() => setPreviewIdx(i)}
                    title={lang === 'en' ? 'Preview file' : '预览文件'}
                    className="inline-flex max-w-[10rem] items-center gap-1.5 rounded-full border border-border bg-secondary/60 px-2.5 py-1 text-xs text-foreground transition-colors hover:border-primary/40 hover:bg-secondary"
                  >
                    <Icon className="h-3 w-3 shrink-0 text-muted-foreground" />
                    <span className="truncate">{a.name}</span>
                    <span
                      role="button"
                      tabIndex={0}
                      onClick={(e) => { e.stopPropagation(); onRemoveAttachment(i); setPreviewIdx(null); }}
                      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); onRemoveAttachment(i); setPreviewIdx(null); } }}
                      className="shrink-0 text-muted-foreground transition-colors hover:text-foreground"
                      aria-label={lang === 'en' ? 'Remove attachment' : '移除附件'}
                    >
                      <X className="h-3 w-3" />
                    </span>
                  </button>
                );
              })}
            </div>
            <div className="flex shrink-0 items-center gap-1">
              {isStreaming ? (
                // While the agent is responding: only the Stop button is
                // visible. The mic (VoiceInput) and the mid-turn Steer
                // button are both hidden — three icons in the corner
                // was visually loud and the user asked for just Stop
                // during the response. For automation runs the Stop
                // button routes through ``onStopAutomation`` so the
                // background executor gets a cooperative cancel signal
                // (the existing by-session poll picks up the new state
                // on the next tick and exits the streaming UI).
                <button
                  type="button"
                  onClick={() => {
                    if (cancelInFlight) return;
                    if (onStopAutomation) {
                      setCancelInFlight(true);
                      try {
                        const r = onStopAutomation();
                        // onStopAutomation may return a promise (the
                        // cancel API call). If it doesn't, clear the
                        // in-flight flag immediately. Either way the
                        // polling loop will exit the streaming UI when
                        // the DB row transitions to cancelled.
                        if (r && typeof r.then === 'function') {
                          r.catch(() => {}).finally(() => setCancelInFlight(false));
                        } else {
                          setCancelInFlight(false);
                        }
                      } catch (e) {
                        setCancelInFlight(false);
                      }
                    } else if (onStop) {
                      onStop();
                    }
                  }}
                  disabled={cancelInFlight}
                  className="inline-flex h-8 w-8 items-center justify-center rounded-full border border-zinc-200 bg-zinc-100 text-zinc-700 transition-colors hover:bg-zinc-200 hover:border-zinc-300 disabled:cursor-not-allowed disabled:opacity-60"
                  title={cancelInFlight
                    ? (lang === 'en' ? 'Cancelling…' : '取消中…')
                    : stopTitleKey}
                  aria-label={cancelInFlight
                    ? (lang === 'en' ? 'Cancelling…' : '取消中…')
                    : stopTitleKey}
                >
                  <Square className="h-3.5 w-3.5 fill-current" />
                </button>
              ) : (
                // Idle: mic + Send button. The mic routes voice
                // transcripts into the input via onChange.
                <>
                  <VoiceInput onTranscript={(text) => onChange(value ? `${value} ${text}` : text)} disabled={disabled} lang={lang} />
                  <button
                    type="button"
                    onClick={handleSend}
                    disabled={disabled || !value.trim()}
                    data-testid="btn-send"
                    title={t.common.send}
                    aria-label={t.common.send}
                    className="inline-flex h-8 w-8 items-center justify-center rounded-full bg-gradient-to-br from-indigo-500 to-violet-600 text-white shadow-md shadow-indigo-500/25 transition-all hover:from-indigo-600 hover:to-violet-700 hover:shadow-indigo-500/40 active:scale-95 disabled:cursor-not-allowed disabled:opacity-40 disabled:shadow-none"
                  >
                    <Send className="h-3.5 w-3.5" />
                  </button>
                </>
              )}
            </div>
          </div>
        </div>
      </div>
      {/* Phase 3: inline preview modal — clicking an attachment chip
          opens the file in a dialog. Reuses the existing FilePreviewer
          which already handles images (img), PDFs (iframe), HTML (sandboxed
          iframe), and Office docs (self-hosted or Office Online). The
          attachment object only carries {name, file_url}; we synthesize
          the file_type from the extension so FilePreviewer routes
          correctly. */}
      {previewIdx !== null && attachments[previewIdx] && (
        <FilePreviewModal
          file={{
            name: attachments[previewIdx].name,
            file_url: attachments[previewIdx].file_url,
            file_type: (attachments[previewIdx].name || '').split('.').pop().toLowerCase(),
          }}
          open
          onOpenChange={(v) => { if (!v) setPreviewIdx(null); }}
        />
      )}
    </div>
  );

}
