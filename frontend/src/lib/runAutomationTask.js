/**
 * Shared "Run Now" helper — one identical behavior from the automation
 * list page and the task detail page.
 *
 * Sequence: ensure-session → POST /run → navigate to /chat?session=<sid>
 *
 * The previous "prefill+autorun" path tried to route the request through
 * the chat agent by injecting a structured "Run Automation Task: ..." prompt
 * into the chatbox and auto-sending it. That path silently no-op'd on
 * mobile (MobileChatPage doesn't handle ?prefill/?autorun) and on any
 * custom agent whose toolset didn't include execute_automation.
 *
 * The reliable path is to fire ``POST /api/automations/{id}/run`` directly
 * and let the dispatcher's ``_notify_chat`` stream the result into the
 * task's owning session. The result lands inline in the chat (assistant
 * bubble with title = task.name, activity_steps mirroring the run, and the
 * full report content) — same UX as a scheduled run, just triggered
 * manually.
 */
import { authFetch } from '@/api/authFetch';
import { registerExecution } from '@/lib/automationStatusBus';

export async function runAutomationTask(task, { navigate, toast, lang = 'en' }) {
  const taskId = task?.id;
  if (!taskId) {
    toast({ title: 'Error', description: 'No task id', variant: 'destructive' });
    return;
  }

  // Step 1: ensure the task has a dedicated chat session.
  // Non-fatal — if it fails, fall through with whatever session_id the
  // task already has.
  let sessionId = task?.session_id;
  try {
    const ensureRes = await authFetch(
      `/api/automations/${taskId}/ensure-session`,
      { method: 'POST' },
    );
    if (ensureRes.ok) {
      const ensureData = await ensureRes.json();
      if (ensureData.session_id) sessionId = ensureData.session_id;
    }
  } catch {
    // Non-fatal — proceed with whatever session_id we have.
  }

  if (!sessionId) {
    toast({
      title: 'Error',
      description: lang === 'en' ? 'No chat session available' : '没有可用的聊天会话',
      variant: 'destructive',
    });
    return;
  }

  // Step 2: fire the run directly. The dispatcher's _notify_chat will
  // stream the result into the task's session — no LLM-mediated handoff,
  // no prefill, no autorun.
  let executionId = null;
  try {
    const runRes = await authFetch(
      `/api/automations/${taskId}/run`,
      { method: 'POST' },
    );
    if (!runRes.ok) {
      toast({
        title: lang === 'en' ? 'Run failed' : '运行失败',
        description: lang === 'en'
          ? `Status ${runRes.status}`
          : `状态码 ${runRes.status}`,
        variant: 'destructive',
      });
      return;
    }
    try {
      const runData = await runRes.json();
      executionId = runData.execution_id || runData.executionId || null;
    } catch {
      // Body wasn't JSON — execution still started, just no id to toast.
    }
  } catch (err) {
    toast({
      title: lang === 'en' ? 'Run failed' : '运行失败',
      description: (err && err.message) || 'Network error',
      variant: 'destructive',
    });
    return;
  }

  // Step 2b: register the execution on the global status bus so the pinned
  // AutomationExecutionStatusDrawer surfaces immediately and starts its 2s
  // polling. Best-effort — the drawer only needs the executionId to poll.
  if (executionId) {
    try {
      registerExecution({
        executionId,
        taskId,
        taskName: task?.name || task?.description || '',
        sessionId,
      });
    } catch {
      // Non-fatal — the toast below still informs the user a run started.
    }
  }

  // Step 3: toast the execution id (truncated for readability) and
  // notify any open /chat listener BEFORE navigation so the chat can
  // restart its session poll + force-reload messages as soon as the new
  // run lands. Without this signal, two failure modes reopen:
  //   1. On a *cold* Chat.jsx mount, the user bubble + empty assistant
  //      bubble written by the dispatcher's `_post_run_request_marker`
  //      may take a full poll cycle to surface in `messages` — leaving
  //      only the generic 3 placeholders visible for ~2.5s.
  //   2. On a *warm* Chat.jsx that's already on `/chat?session=<sid>`
  //      (e.g. scheduled run + the user triggering Run Now in another
  //      tab), the by-session poller has gone into stable-terminal
  //      sleep and would never wake up on its own — the new user bubble
  //      would sit invisible at the bottom of the messages array.
  //
  // The event is best-effort: if no listener is mounted we still fall
  // back to navigation + the existing polling loop.
  const idShort = executionId ? executionId.slice(0, 8) : '';
  toast({
    title: lang === 'en' ? 'Execution started' : '执行已开始',
    description: idShort
      ? (lang === 'en' ? `id: ${idShort}…` : `编号：${idShort}…`)
      : (task?.name || ''),
  });
  try {
    window.dispatchEvent(new CustomEvent('automation:run-started', {
      detail: { sessionId, executionId, taskId },
    }));
  } catch {
    // SSR or window-less env — no-op, navigation below is the fallback.
  }
  // (2026-08-31) Carry the automation context in the navigation URL —
  // the user wants automation sessions to read
  // ``?automationId=<id>&agentName=automation_agent&projectName=<p>&session=<sid>``
  // so a refresh keeps the automation identity visible and re-binds the
  // same automation_agent. The /chat ?session= handler rewrites
  // ``session`` → ``conv`` and preserves the other params.
  const navParams = new URLSearchParams({ session: sessionId });
  if (taskId) navParams.set('automationId', taskId);
  navParams.set('agentName', 'automation_agent');
  if (task?.project && task.project !== 'global') {
    navParams.set('projectName', task.project);
  }
  navigate(`/chat?${navParams.toString()}`);
}
