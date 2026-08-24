/**
 * ActivityRail — Execution timeline showing the FSM state progression.
 *
 * Displays the Synexia FSM states as a visual rail:
 * INIT → GOAL → CONTEXT → PLAN → GATE → ACT → OBSERVE → VERIFY → FINALIZE
 *
 * Each state shows its status (done, current, pending) and a brief
 * description.  Observations are shown as expandable items under ACT/OBSERVE.
 */

import { useState } from 'react';
import {
  Loader2, CheckCircle2, Circle, Brain, Search, ClipboardList,
  Shield, Zap, Eye, BadgeCheck, Flag, ChevronDown, ChevronRight,
  AlertCircle, BookOpen, Sparkles,
} from 'lucide-react';

const FSM_STATES = [
  { key: 'init', label: 'Init', icon: Circle, desc: 'Execution created' },
  { key: 'goal', label: 'Goal', icon: Brain, desc: 'Understanding your request' },
  { key: 'context', label: 'Context', icon: Search, desc: 'Gathering context' },
  { key: 'plan', label: 'Plan', icon: ClipboardList, desc: 'Creating execution plan' },
  { key: 'gate', label: 'Gate', icon: Shield, desc: 'Policy evaluation' },
  { key: 'act', label: 'Act', icon: Zap, desc: 'Executing plan' },
  { key: 'observe', label: 'Observe', icon: Eye, desc: 'Recording results' },
  { key: 'verify', label: 'Verify', icon: BadgeCheck, desc: 'Validating outputs' },
  { key: 'finalize', label: 'Finalize', icon: Flag, desc: 'Computing confidence' },
];

function factorScore(factor) {
  if (!factor || typeof factor !== 'object') return 0;
  const raw = factor.score ?? factor.overall_score ?? factor.confidence ?? factor.completeness_score ?? 0;
  const score = Number(raw);
  if (!Number.isFinite(score)) return 0;
  return Math.max(0, Math.min(1, score));
}

function SelectedSkillValidation({ validation }) {
  if (!validation || typeof validation !== 'object') return null;
  const ok = validation.is_ok !== false;
  const score = factorScore(validation);
  const missing = validation.missing_elements || [];
  const issues = validation.issues || [];

  return (
    <div className={`rounded-lg border px-3 py-2 text-xs ${ok ? 'border-green-500/20 bg-green-50/60' : 'border-amber-500/30 bg-amber-50/70'}`}>
      <div className="flex items-center gap-2">
        {ok ? <CheckCircle2 className="h-3.5 w-3.5 text-green-600" /> : <AlertCircle className="h-3.5 w-3.5 text-amber-600" />}
        <span className="font-medium text-foreground">Selected skill validation</span>
        <span className={`ml-auto font-mono ${ok ? 'text-green-600' : 'text-amber-600'}`}>{(score * 100).toFixed(0)}%</span>
      </div>
      {validation.skill_name && (
        <div className="mt-1 text-[11px] text-muted-foreground">Skill: {validation.skill_name}</div>
      )}
      {missing.length > 0 && (
        <div className="mt-1 text-[11px] text-amber-700">Missing: {missing.join(', ')}</div>
      )}
      {issues.length > 0 && (
        <div className="mt-1 text-[11px] text-muted-foreground">{issues[0]}</div>
      )}
    </div>
  );
}

export default function ActivityRail({ execution }) {
  const [expandedObs, setExpandedObs] = useState({});

  if (!execution) return null;

  const currentState = execution.current_state || 'init';
  const currentIndex = FSM_STATES.findIndex(s => s.key === currentState);
  const isFailed = currentState === 'fail';
  const isDone = currentState === 'done';
  const observations = execution.observations || [];
  const confidence = execution.confidence_score;

  return (
    <div className="rounded-xl border border-border bg-card overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5 bg-secondary/30">
        <span className="text-sm font-medium text-foreground">Activity</span>
        {isDone && confidence !== null && confidence !== undefined && (
          <div className="flex items-center gap-1.5">
            <span className="text-[11px] text-muted-foreground">Confidence</span>
            <span className={`text-xs font-bold ${confidence >= 0.8 ? 'text-green-600' : confidence >= 0.5 ? 'text-amber-500' : 'text-red-500'
              }`}>
              {(confidence * 100).toFixed(0)}%
            </span>
          </div>
        )}
      </div>

      {/* FSM State Rail */}
      <div className="p-3">
        <div className="space-y-1">
          {FSM_STATES.map((state, i) => {
            const StateIcon = state.icon;
            const isCompleted = isDone || i < currentIndex;
            const isCurrent = !isDone && !isFailed && i === currentIndex;
            const isPending = !isCompleted && !isCurrent;

            return (
              <div
                key={state.key}
                className={`flex items-center gap-2.5 rounded-lg px-2 py-1.5 ${isCurrent ? 'bg-blue-50' : isCompleted ? 'bg-green-50/50' : ''
                  }`}
              >
                <div className="flex h-6 w-6 shrink-0 items-center justify-center">
                  {isCompleted ? (
                    <CheckCircle2 className="h-4 w-4 text-green-500" />
                  ) : isCurrent ? (
                    <Loader2 className="h-4 w-4 animate-spin text-blue-500" />
                  ) : (
                    <Circle className="h-4 w-4 text-gray-300" />
                  )}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className={`text-xs font-medium ${isCurrent ? 'text-blue-600' : isCompleted ? 'text-foreground' : 'text-muted-foreground'
                      }`}>
                      {state.label}
                    </span>
                    {isCurrent && (
                      <span className="text-[10px] text-blue-400">• {state.desc}</span>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        {/* Error state */}
        {isFailed && (
          <div className="mt-2 flex items-center gap-2 rounded-lg bg-red-50 px-2 py-1.5">
            <AlertCircle className="h-4 w-4 text-red-500" />
            <span className="text-xs font-medium text-red-600">Execution failed</span>
          </div>
        )}
      </div>

      {/* Observations */}
      {observations.length > 0 && (
        <div className="border-t border-border p-3">
          <p className="mb-2 text-[11px] font-medium text-muted-foreground">
            Observations ({observations.length})
          </p>
          <div className="space-y-1">
            {observations.map((obs, i) => {
              const isExpanded = expandedObs[i];
              const isSkillCall = obs.observation_type === 'skill_call';
              const skillName = isSkillCall
                ? (obs.tool_name || (obs.result_data && obs.result_data.name) || 'skill')
                : null;
              return (
                <div
                  key={i}
                  className={`rounded-lg px-2 py-1.5 text-xs ${
                    isSkillCall
                      ? 'border-l-[3px] border-l-indigo-500 bg-indigo-50/60'
                      : obs.success ? 'bg-green-50/50' : 'bg-red-50/50'
                  }`}
                >
                  <div className="flex items-center gap-1.5">
                    {isSkillCall ? (
                      <BookOpen className="h-3 w-3 shrink-0 text-indigo-500" />
                    ) : obs.success ? (
                      <CheckCircle2 className="h-3 w-3 shrink-0 text-green-500" />
                    ) : (
                      <AlertCircle className="h-3 w-3 shrink-0 text-red-500" />
                    )}
                    <button
                      onClick={() => setExpandedObs({ ...expandedObs, [i]: !isExpanded })}
                      className="flex items-center gap-1 text-foreground hover:text-foreground"
                    >
                      {isExpanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                      {isSkillCall ? (
                        <span className="font-semibold text-indigo-700">
                          {obs.success ? 'Loaded skill' : 'Skill load failed'}: {skillName}
                        </span>
                      ) : (
                        <span className="font-medium">{obs.tool_name || obs.observation_type}</span>
                      )}
                    </button>
                    {obs.duration_ms && (
                      <span className="ml-auto text-[10px] text-muted-foreground">{obs.duration_ms}ms</span>
                    )}
                  </div>
                  {isSkillCall && obs.success && !isExpanded && (
                    <p className="mt-0.5 pl-7 text-[10px] text-indigo-500/70">
                      Following methodology: {skillName}
                    </p>
                  )}
                  {isExpanded && obs.result_text && (
                    <pre className="mt-1 whitespace-pre-wrap pl-6 text-[10px] text-muted-foreground">
                      {obs.result_text.slice(0, 500)}
                    </pre>
                  )}
                  {isExpanded && obs.error_message && (
                    <pre className="mt-1 whitespace-pre-wrap pl-6 text-[10px] text-red-400">
                      {obs.error_message}
                    </pre>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Confidence factors */}
      {isDone && execution.confidence_factors && (
        <div className="border-t border-border p-3">
          <p className="mb-2 text-[11px] font-medium text-muted-foreground">Confidence Factors</p>
          <SelectedSkillValidation validation={execution.confidence_factors.selected_skill_validation} />
          <div className="space-y-1">
            {Object.entries(execution.confidence_factors).filter(([key]) => key !== 'selected_skill_validation').map(([key, factor]) => {
              const score = factorScore(factor);
              return (
                <div key={key} className="flex items-center gap-2 text-[11px]">
                  <span className="w-32 shrink-0 text-muted-foreground">{key.replace(/_/g, ' ')}</span>
                  <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-secondary">
                    <div
                      className={`h-full ${score >= 0.8 ? 'bg-green-500' : score >= 0.5 ? 'bg-amber-500' : 'bg-red-500'
                        }`}
                      style={{ width: `${score * 100}%` }}
                    />
                  </div>
                  <span className="w-8 shrink-0 text-right font-mono text-muted-foreground">
                    {(score * 100).toFixed(0)}%
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
