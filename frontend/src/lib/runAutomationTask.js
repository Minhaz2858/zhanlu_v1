/**
 * Shared "Run Now" helper — one identical behavior from the automation
 * list page and the task detail page.
 *
 * Sequence: ensure-session (non-fatal) → POST /api/automations/{id}/run
 * → toast the execution id → navigate to /chat?session=<sid>.
 *
 * The navigation URL NEVER carries `prefill` or `autorun`. The old
 * LLM-dependent deep-link flow (pre-fill a "Run Automation Task:" prompt
 * and expect the chat agent to call `execute_automation`) silently
 * no-op'd for custom agents whose toolset lacks that tool. The direct
 * `/run` API sidesteps the agent entirely: the dispatcher fires the
 * executor, which posts the visible request bubble server-side and
 * streams the result into the dedicated session.
 *
 * Failure path: destructive toast, no navigation.
 */
import { authFetch } from '@/api/authFetch';

export async function runAutomationTask(task, { navigate, toast, lang = 'en' }) {
  const taskId = task?.id;
  if (!taskId) {
    toast({ title: 'Error', description: 'No task id', variant: 'destructive' });
    return;
  }

  // Step 1: ensure the task has a dedicated chat session named after it.
  // Non-fatal — if it fails, fall through with whatever session_id the
  // task already has (or none).
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

  // Step 2: fire the run directly via the API. The dispatcher's
  // trigger_now fires the executor, which posts the request bubble
  // server-side and writes the result back to task.session_id.
  try {
    const runRes = await authFetch(
      `/api/automations/${taskId}/run`,
      { method: 'POST' },
    );
    if (!runRes.ok) throw new Error(`HTTP ${runRes.status}`);
    const runData = await runRes.json();

    // Step 3: toast the execution id and land in the dedicated chat.
    toast({
      title: lang === 'en' ? 'Execution started' : '已开始执行',
      description: runData.execution_id
        ? `${lang === 'en' ? 'Execution id:' : '执行 id:'} ${runData.execution_id.slice(0, 8)}`
        : undefined,
    });
    if (sessionId) {
      navigate(`/chat?session=${encodeURIComponent(sessionId)}`);
    }
  } catch (e) {
    toast({ title: 'Error', description: e.message, variant: 'destructive' });
  }
}
