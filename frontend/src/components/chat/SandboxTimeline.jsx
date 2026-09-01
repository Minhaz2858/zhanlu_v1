/**
 * SandboxTimeline — Live execution timeline for sandbox jobs.
 *
 * Displays a real-time timeline of sandbox execution events:
 * job_queued → job_started → command_started → stdout → file_created → job_completed
 *
 * Uses SSE (Server-Sent Events) for real-time streaming, with polling fallback.
 * Renders like Claude Code's execution view — each event is a timeline entry
 * with an icon, message, and optional expandable detail.
 */

import { useState, useEffect, useRef, useCallback } from 'react';
import { formatTimeOfDay } from '@/lib/time';
import {
  Loader2, CheckCircle2, AlertCircle, Clock, Terminal,
  FilePlus, FileText, PlayCircle, StopCircle, RefreshCw,
  ChevronDown, ChevronRight, Cpu, Container,
} from 'lucide-react';
import { authFetch } from '@/api/authFetch';

const API_BASE = '/api';

// Event type → icon + color mapping
const EVENT_META = {
  job_queued: { icon: Clock, color: 'text-gray-500', bg: 'bg-gray-100' },
  job_started: { icon: PlayCircle, color: 'text-blue-500', bg: 'bg-blue-100' },
  input_prepared: { icon: FilePlus, color: 'text-cyan-500', bg: 'bg-cyan-100' },
  command_started: { icon: Terminal, color: 'text-purple-500', bg: 'bg-purple-100' },
  stdout: { icon: Terminal, color: 'text-gray-400', bg: 'bg-gray-50' },
  stderr: { icon: Terminal, color: 'text-red-400', bg: 'bg-red-50' },
  file_created: { icon: FilePlus, color: 'text-green-500', bg: 'bg-green-100' },
  file_stored: { icon: CheckCircle2, color: 'text-green-500', bg: 'bg-green-100' },
  validation_started: { icon: RefreshCw, color: 'text-amber-500', bg: 'bg-amber-100' },
  validation_passed: { icon: CheckCircle2, color: 'text-green-500', bg: 'bg-green-100' },
  validation_failed: { icon: AlertCircle, color: 'text-red-500', bg: 'bg-red-100' },
  job_completed: { icon: CheckCircle2, color: 'text-green-600', bg: 'bg-green-100' },
  job_failed: { icon: AlertCircle, color: 'text-red-500', bg: 'bg-red-100' },
  job_timeout: { icon: Clock, color: 'text-orange-500', bg: 'bg-orange-100' },
  stream_end: { icon: StopCircle, color: 'text-gray-400', bg: 'bg-gray-100' },
};

const JOB_STATUS_META = {
  queued: { icon: Clock, color: 'text-gray-500', label: 'Queued' },
  running: { icon: Loader2, color: 'text-blue-500', label: 'Running', spin: true },
  completed: { icon: CheckCircle2, color: 'text-green-600', label: 'Completed' },
  failed: { icon: AlertCircle, color: 'text-red-500', label: 'Failed' },
  timeout: { icon: Clock, color: 'text-orange-500', label: 'Timeout' },
  cancelled: { icon: StopCircle, color: 'text-gray-500', label: 'Cancelled' },
};

export default function SandboxTimeline({ jobId, onComplete }) {
  const [events, setEvents] = useState([]);
  const [jobStatus, setJobStatus] = useState('queued');
  const [expanded, setExpanded] = useState({});
  const [commands, setCommands] = useState([]);
  const eventSourceRef = useRef(null);
  const pollIntervalRef = useRef(null);
  const lastSeqRef = useRef(-1);

  const handleEvent = useCallback((event) => {
    setEvents((prev) => [...prev, event]);
    lastSeqRef.current = Math.max(lastSeqRef.current, event.seq);

    if (event.event_type === 'stream_end') {
      setJobStatus(event.data?.job_status || 'completed');
      if (onComplete) onComplete(event.data?.job_status);
    }
  }, [onComplete]);

  const startSSE = useCallback(() => {
    if (!jobId) return;

    // Close existing connection
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
    }

    const url = `${API_BASE}/sandbox/jobs/${jobId}/events/stream`;
    const source = new EventSource(url);
    eventSourceRef.current = source;

    source.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        handleEvent(data);
      } catch (err) {
        console.error('SSE parse error:', err);
      }
    };

    source.onerror = () => {
      // Fallback to polling when SSE fails
      source.close();
      eventSourceRef.current = null;
      startPolling();
    };
  }, [jobId, handleEvent]);

  const startPolling = useCallback(() => {
    if (pollIntervalRef.current) return;

    const poll = async () => {
      try {
        const res = await authFetch(`${API_BASE}/sandbox/jobs/${jobId}/events?after_seq=${lastSeqRef.current}`);
        if (!res.ok) return;
        const newEvents = await res.json();
        if (newEvents.length > 0) {
          newEvents.forEach(handleEvent);
        }

        // Check job status
        const jobRes = await authFetch(`${API_BASE}/sandbox/jobs/${jobId}`);
        if (jobRes.ok) {
          const job = await jobRes.json();
          setJobStatus(job.status);
          setCommands(job.commands || []);

          if (['completed', 'failed', 'timeout', 'cancelled'].includes(job.status)) {
            if (pollIntervalRef.current) {
              clearInterval(pollIntervalRef.current);
              pollIntervalRef.current = null;
            }
            if (onComplete) onComplete(job.status);
          }
        }
      } catch (err) {
        // Silent retry
      }
    };

    poll();
    pollIntervalRef.current = setInterval(poll, 1000);
  }, [jobId, handleEvent, onComplete]);

  // Try SSE first, fall back to polling
  useEffect(() => {
    startSSE();
    return () => {
      if (eventSourceRef.current) eventSourceRef.current.close();
      if (pollIntervalRef.current) clearInterval(pollIntervalRef.current);
    };
  }, [startSSE]);

  const statusMeta = JOB_STATUS_META[jobStatus] || JOB_STATUS_META.queued;
  const StatusIcon = statusMeta.icon;
  const isRunning = jobStatus === 'running' || jobStatus === 'queued';

  return (
    <div className="rounded-xl border border-border bg-card overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-2 border-b border-border px-4 py-2.5 bg-secondary/30">
        <Container className="h-4 w-4 text-muted-foreground" />
        <span className="text-sm font-medium text-foreground">Sandbox Execution</span>
        <span className="text-[11px] text-muted-foreground font-mono">{jobId?.slice(0, 8)}</span>
        <div className="ml-auto flex items-center gap-1.5">
          <StatusIcon className={`h-3.5 w-3.5 ${statusMeta.color} ${statusMeta.spin ? 'animate-spin' : ''}`} />
          <span className={`text-xs font-medium ${statusMeta.color}`}>{statusMeta.label}</span>
        </div>
      </div>

      {/* Timeline */}
      <div className="max-h-[400px] overflow-y-auto p-3">
        {events.length === 0 && isRunning && (
          <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Waiting for execution to start...
          </div>
        )}

        <div className="space-y-1">
          {events.map((event, i) => {
            const meta = EVENT_META[event.event_type] || EVENT_META.stdout;
            const EventIcon = meta.icon;
            const isExpanded = expanded[i];
            const hasDetail = event.data || event.message;

            return (
              <div
                key={i}
                className={`flex items-start gap-2 rounded-lg px-2 py-1.5 text-xs ${meta.bg}`}
              >
                <EventIcon className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${meta.color}`} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1">
                    {hasDetail && (
                      <button
                        onClick={() => setExpanded({ ...expanded, [i]: !isExpanded })}
                        className="shrink-0 text-muted-foreground hover:text-foreground"
                      >
                        {isExpanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                      </button>
                    )}
                    <span className="font-medium text-foreground">{event.event_type}</span>
                    {event.timestamp && (
                      <span className="ml-auto text-[10px] text-muted-foreground">
                        {formatTimeOfDay(event.timestamp)}
                      </span>
                    )}
                  </div>
                  {event.message && (
                    <p className="mt-0.5 text-muted-foreground">{event.message}</p>
                  )}
                  {isExpanded && event.data && (
                    <pre className="mt-1 overflow-x-auto rounded bg-secondary p-2 text-[10px] font-mono">
                      {JSON.stringify(event.data, null, 2)}
                    </pre>
                  )}
                </div>
              </div>
            );
          })}
        </div>

        {/* Commands section */}
        {commands.length > 0 && (
          <div className="mt-3 border-t border-border pt-3">
            <div className="mb-2 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
              <Terminal className="h-3.5 w-3.5" /> Commands ({commands.length})
            </div>
            <div className="space-y-1">
              {commands.map((cmd, i) => (
                <div key={i} className="rounded-lg bg-secondary/50 p-2 font-mono text-[11px]">
                  <div className="flex items-center gap-1.5">
                    <span className="text-purple-500">$</span>
                    <span className="text-foreground">{cmd.command}</span>
                    {cmd.exit_code !== null && (
                      <span className={`ml-auto ${cmd.exit_code === 0 ? 'text-green-500' : 'text-red-500'}`}>
                        exit={cmd.exit_code}
                      </span>
                    )}
                  </div>
                  {cmd.stdout && (
                    <pre className="mt-1 whitespace-pre-wrap text-muted-foreground">{cmd.stdout.slice(0, 500)}</pre>
                  )}
                  {cmd.stderr && (
                    <pre className="mt-1 whitespace-pre-wrap text-red-400">{cmd.stderr.slice(0, 500)}</pre>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
