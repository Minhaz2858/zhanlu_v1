import { Section, Field, inputCls, textareaCls } from './AgentParts';
import ProjectSelector from '@/components/automation/ProjectSelector';
import LlmModelSelector from '@/components/chat/LlmModelSelector';
import { UserCog } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import { useProjectSync } from '@/lib/useProjectSync';

/**
 * RoleSection — agent name / description / project assignment.
 *
 * The Project picker writes BOTH `project_id` (the new FK column) and
 * `project` (the legacy name string) so the new project_id-filtered
 * ProjectDetail page can locate this agent.
 *
 * It also calls `syncProjectMembership` to mirror the change into the
 * `project_agents` association table so the agent is listed in the new
 * project's Agents section (many-to-many membership).
 */
export default function RoleSection({ form, update, t, isRoot }) {
  const { lang } = useLanguage();
  const { resolveProjectChange, syncProjectMembership } = useProjectSync();
  return (
    <Section title={t.agentConfig.role} desc={t.agentConfig.roleDesc} icon={UserCog}>
      <div className="space-y-4">
        <Field label={t.agentConfig.name} hint={t.agentConfig.nameHint}>
          <input
            value={form.name}
            onChange={(e) => update({ name: e.target.value })}
            className={inputCls}
            placeholder={t.agentConfig.namePh}
          />
        </Field>
        <Field label={t.agentConfig.description} hint={t.agentConfig.descHint}>
          <textarea
            value={form.description}
            onChange={(e) => update({ description: e.target.value })}
            rows={2}
            className={`${textareaCls} resize-none`}
            placeholder={t.agentConfig.descPh}
          />
        </Field>
        {isRoot && (
          <Field label={t.agentConfig.project} hint={t.agentConfig.projectHint}>
            <ProjectSelector
              value={form.project_id || form.project || 'global'}
              onChange={(v) => {
                resolveProjectChange(v, update);
                // Mirror into project_agents so this agent appears in
                // the new project's Agents list (many-to-many).
                if (form.id) syncProjectMembership(form.id, v);
              }}
            />
          </Field>
        )}

        {/* Model binding — visible to admins. Non-admin edits to company-
            scoped agents show locked badge (Project still wins at runtime). */}
        <LlmModelSelector
          value={form.llm_model_id || null}
          onChange={(id) => update({ llm_model_id: id })}
          disabled={!isRoot}
        />

      </div>
    </Section>
  );
}
