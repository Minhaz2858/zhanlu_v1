import { ShieldCheck, Gauge } from 'lucide-react';
import { Section, Field, Toggle, inputCls } from './AgentParts';

export default function ControllabilitySection({ form, update, t }) {
  return (
    <Section title={t.agentConfig.controllability} desc={t.agentConfig.controlDesc} icon={ShieldCheck}>
      <div className="space-y-5">
        <div className="grid grid-cols-2 gap-4">
          <Field label={t.agentConfig.maxCalls} hint={t.agentConfig.maxCallsHint}>
            <input
              type="number"
              min="1"
              value={form.max_call_count}
              onChange={(e) => update({ max_call_count: parseInt(e.target.value) || 0 })}
              className={inputCls}
            />
          </Field>
          <Field label={t.agentConfig.maxRetries} hint={t.agentConfig.maxRetriesHint}>
            <input
              type="number"
              min="0"
              value={form.max_retries}
              onChange={(e) => update({ max_retries: parseInt(e.target.value) || 0 })}
              className={inputCls}
            />
          </Field>
        </div>
      </div>
    </Section>
  );
}