// Hook that mirrors the automationStatusBus snapshot into React state and
// drives the 2s polling of any in-flight executions against the lightweight
// ``GET /api/automations/executions/{id}/status`` endpoint.
//
// Polling is tolerant of the early race where the dispatcher has not yet
// committed the execution row: the endpoint returns 200 ``{exists:false}``
// (see backend router) and, for safety, a network/404 error is also swallowed
// — the entry just keeps its last-known status and the next tick retries.
import { useEffect, useRef, useState } from 'react';
import { authFetch } from '@/api/authFetch';
import {
  subscribe,
  getEntries,
  updateExecution,
  TERMINAL_EXECUTION_STATUSES,
} from './automationStatusBus';

const POLL_INTERVAL_MS = 2000;

export function useAutomationExecutionStatus() {
  const [entries, setEntries] = useState(getEntries());
  const activeIdsRef = useRef('');

  // 1. Mirror bus snapshot into React state.
  useEffect(() => subscribe(setEntries), []);

  // 2. Poll only while there is at least one non-terminal (in-flight) entry.
  //    We key the effect on the *sorted, deduped* list of active ids so a
  //    status flip to terminal tears down the interval without re-triggering
  //    on every polled field (steps/skill_calls keep mutating while running).
  const active = entries.filter((e) => !TERMINAL_EXECUTION_STATUSES.has(e.status));
  const activeKey = active.map((e) => e.executionId).sort().join(',');

  useEffect(() => {
    if (!activeKey) return;
    let cancelled = false;

    const pollOnce = async () => {
      await Promise.all(
        active.map(async (entry) => {
          try {
            const res = await authFetch(
              `/api/automations/executions/${entry.executionId}/status`,
            );
            if (!res.ok) return; // tolerate transient 404/5xx — retry next tick
            const data = await res.json();
            if (cancelled) return;
            // exists:false => keep polling; otherwise merge the snapshot.
            updateExecution(entry.executionId, data && data.exists !== false ? data : null);
          } catch {
            // Network error — tolerate, retry on the next tick.
          }
        }),
      );
    };

    pollOnce();
    const id = setInterval(pollOnce, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeKey]);

  return entries;
}
