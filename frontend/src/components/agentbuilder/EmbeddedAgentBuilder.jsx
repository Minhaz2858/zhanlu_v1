import { useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { useAgentBuilder } from '@/hooks/useAgentBuilder';
import BuilderMessageBubble from '@/components/agentbuilder/BuilderMessageBubble';
import ThinkingIndicator from '@/components/agentbuilder/ThinkingIndicator';
import AgentToolsPanel from '@/components/agentbuilder/AgentToolsPanel';
import ArtifactPreviewSheet from '@/components/chat/ArtifactPreviewSheet';
import AgentMarkdown from '@/components/agent/AgentMarkdown';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Bot, Send, Loader2, ArrowLeft, ArrowRight, CheckCircle2, Shield, ShieldAlert, ShieldCheck, Wrench } from 'lucide-react';

/**
 * EmbeddedAgentBuilder — a slim version of the AgentBuilder page that
 * renders inline inside MySpace. It reuses the useAgentBuilder hook for
 * all state/SSE logic but drops the left-hand conversation list (this is
 * a single-build scenario) and adds a "back to my agents" breadcrumb.
 *
 * Props:
 *   prefill  {string}  Initial prompt to auto-send on mount.
 *   onClose  {func}    Called when the user clicks the back button.
 */
export default function EmbeddedAgentBuilder({ prefill, onClose }) {
  const { t, lang } = useLanguage();
  const navigate = useNavigate();
  const sentPrefillRef = useRef(false);

  const builder = useAgentBuilder({
    t,
    onAgentCreated: () => { /* success dialog handled locally */ },
  });
  const {
    messages,
    streamingContent,
    input,
    loading,
    initializing,
    createdAgent,
    preflightResult,
    decisionPayload,
    openArtifact,
    scrollRef,
    inputRef,
    startNewChat,
    handleSend,
    handleSelectOption,
    setDecisionPayload,
    setCreatedAgent,
    setCreatedAgentValue,
    handleIncomingMessages,
    setLoading,
    setOpenArtifact,
    activeId,
  } = builder;

  // Auto-send the prefill prompt once on mount.
  useEffect(() => {
    if (prefill && !sentPrefillRef.current) {
      sentPrefillRef.current = true;
      startNewChat(prefill);
    }
  }, [prefill, startNewChat]);

  const showEmpty = messages.length === 0 && !streamingContent && !loading && !initializing;

  const renderInputArea = () => (
    <div className="rounded-xl border border-border bg-card shadow-sm transition focus-within:ring-2 focus-within:ring-primary/30">
      <textarea
        ref={inputRef}
        value={input}
        onChange={(e) => builder.setInput(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(input); } }}
        placeholder={t.agentBuilder.placeholder}
        rows={2}
        disabled={loading || initializing}
        className="w-full resize-none bg-transparent px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none"
      />
      <div className="flex items-center justify-between px-3 pb-2">
        <span className="text-[11px] text-muted-foreground">{t.agentBuilder.madeBy}</span>
        <button onClick={() => handleSend(input)} disabled={loading || initializing || !input.trim()} className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-opacity disabled:opacity-40">
          {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />} {t.common.send}
        </button>
      </div>
    </div>
  );

  return (
    <div className="flex h-full flex-col">
      {/* Breadcrumb / back bar */}
      <div className="flex items-center gap-3 border-b border-border px-6 py-3">
        <button
          onClick={onClose}
          className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" /> {t.mySpace.backToAgents}
        </button>
        <div className="h-3 w-px bg-border" />
        <div className="flex items-center gap-1.5">
          <Bot className="h-3.5 w-3.5 text-primary" />
          <span className="text-xs font-medium text-foreground">{t.agentBuilder.title}</span>
        </div>
      </div>

      <div className="flex min-h-0 flex-1 flex-col">
        {showEmpty ? (
          <div className="flex flex-1 flex-col items-center justify-center px-6 pb-12">
            <div className="mb-5 flex h-12 w-12 items-center justify-center rounded-2xl bg-primary/10">
              <Bot className="h-6 w-6 text-primary" />
            </div>
            <p className="mt-2 max-w-lg text-center text-sm text-muted-foreground">{t.agentBuilder.description}</p>
            <div className="mt-6 w-full max-w-2xl">
              {renderInputArea()}
            </div>
          </div>
        ) : (
          <>
            <div ref={scrollRef} className="flex-1 min-h-0 space-y-6 overflow-y-auto px-6 py-6">
              <div className="mx-auto max-w-3xl space-y-6">
                {messages.map((m, i) => (
                  <BuilderMessageBubble
                    key={i}
                    message={m}
                    onOptionSelect={handleSelectOption}
                    conversationId={builder.activeId}
                    hideInternalTools={!!decisionPayload}
                    onDecisionConfirmed={(result) => {
                      setDecisionPayload(null);
                      const agent = result && result.agent;
                      if (agent && agent.id) {
                        setCreatedAgentValue({ id: agent.id, name: agent.name || '', skills: agent.skills || [] });
                      } else {
                        base44.agents.getConversation(activeId).then((c) => {
                          handleIncomingMessages(c.messages || []);
                        }).catch(() => {});
                      }
                      setLoading(false);
                    }}
                    onDecisionCancelled={() => setDecisionPayload(null)}
                    onArtifactPreview={setOpenArtifact}
                  />
                ))}
                {streamingContent && (
                  <div className="flex animate-slide-up gap-3">
                    <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-border bg-secondary text-primary">
                      <Bot className="h-4 w-4" />
                    </div>
                    <div className="flex max-w-[85%] flex-col items-start">
                      <div className="rounded-2xl rounded-tl-sm border border-border bg-card px-4 py-2.5 text-sm text-foreground">
                        <AgentMarkdown onOptionSelect={handleSelectOption}>{streamingContent}</AgentMarkdown>
                        <span className="ml-0.5 inline-block h-3 w-0.5 animate-pulse bg-foreground align-middle" />
                      </div>
                    </div>
                  </div>
                )}
                {(loading || initializing) && !streamingContent && (
                  <ThinkingIndicator lang={lang} />
                )}
              </div>
            </div>
            <div className="border-t border-border bg-card/50 px-6 py-4">
              <div className="mx-auto max-w-3xl">
                {renderInputArea()}
              </div>
            </div>
          </>
        )}
      </div>

      {/* Success dialog */}
      <Dialog open={!!createdAgent} onOpenChange={(open) => { if (!open) setCreatedAgent(null); }}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-green-500/10">
                <CheckCircle2 className="h-5 w-5 text-green-500" />
              </div>
              <div>
                <DialogTitle>{t.agentBuilder.success.title}</DialogTitle>
                <DialogDescription>{t.agentBuilder.success.desc}</DialogDescription>
              </div>
            </div>
          </DialogHeader>
          {createdAgent?.name && (
            <div className="rounded-lg border border-border bg-secondary/40 p-3">
              <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">{t.agentBuilder.success.agentName}</div>
              <div className="flex items-center gap-2">
                <Bot className="h-4 w-4 text-primary" />
                <span className="text-sm font-medium text-foreground">{createdAgent.name}</span>
              </div>
            </div>
          )}
          {preflightResult && (
            <div className="rounded-lg border border-border bg-secondary/40 p-3">
              <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Harness Agent</div>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  {preflightResult.status === 'ready' ? (
                    <ShieldCheck className="h-4 w-4 text-green-500" />
                  ) : preflightResult.status === 'warning' ? (
                    <ShieldAlert className="h-4 w-4 text-amber-500" />
                  ) : (
                    <Shield className="h-4 w-4 text-red-500" />
                  )}
                  <span className="text-[11px] font-medium text-foreground">
                    Preflight: {preflightResult.status === 'ready' ? 'Ready' : preflightResult.status === 'warning' ? 'Warnings' : 'Blocked'}
                  </span>
                </div>
                <span className="rounded-full bg-secondary px-2 py-0.5 text-[10px] font-medium text-muted-foreground">
                  {preflightResult.summary}
                </span>
              </div>
            </div>
          )}
          {createdAgent && (
            <div className="mt-2">
              <AgentToolsPanel
                agent={createdAgent}
                onAddTools={() => { const id = createdAgent.id; setCreatedAgent(null); if (id) navigate(`/my-space/agent/${id}`); }}
              />
            </div>
          )}
          <DialogFooter className="flex-col gap-2 sm:flex-row sm:flex-wrap">
            <Button variant="outline" onClick={() => setCreatedAgent(null)} className="w-full sm:w-auto">
              {t.agentBuilder.success.stayHere}
            </Button>
            <Button
              variant="outline"
              onClick={() => { const id = createdAgent?.id; setCreatedAgent(null); if (id) navigate(`/my-space/agent/${id}#agent-section-prompt`); }}
              className="w-full sm:w-auto"
            >
              <Wrench className="mr-1.5 h-3.5 w-3.5" />
              {t.agentBuilder.success.openPrompt}
            </Button>
            <Button onClick={() => { const id = createdAgent?.id; setCreatedAgent(null); if (id) navigate(`/my-space/agent/${id}`); }} className="w-full sm:w-auto">
              {t.agentBuilder.success.goConfig} <ArrowRight className="ml-1.5 h-3.5 w-3.5" />
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ArtifactPreviewSheet artifact={openArtifact} open={!!openArtifact} onOpenChange={(open) => { if (!open) setOpenArtifact(null); }} />
    </div>
  );
}
