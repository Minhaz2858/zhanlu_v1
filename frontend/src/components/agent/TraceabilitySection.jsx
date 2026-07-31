import { Activity } from 'lucide-react';
import { Section, Field, Toggle, inputCls } from './AgentParts';

const LOG_LEVELS = [
  { value: 'debug', label: 'Debug' },
  { value: 'info', label: 'Info' },
  { value: 'warn', label: 'Warn' },
  { value: 'error', label: 'Error' },
];

export default function TraceabilitySection({ form, update, t }) {
  return (
    <Section title={t.agentConfig.trace} desc={t.agentConfig.traceDesc} icon={Activity}>
      <div className="space-y-4">
        <Field label={t.agentConfig.logLevel} hint={t.agentConfig.logLevelHint}>
          <select
            value={form.log_level}
            onChange={(e) => update({ log_level: e.target.value })}
            className={inputCls}
          >
            {LOG_LEVELS.map((l) => <option key={l.value} value={l.value}>{l.label}</option>)}
          </select>
        </Field>
      </div>
    </Section>
  );
}