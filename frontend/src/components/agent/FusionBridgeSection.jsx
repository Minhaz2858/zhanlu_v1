import { Box } from 'lucide-react';
import { Section, Field, inputCls } from './AgentParts';

// Fusion 360 bridge endpoint — lives in tool_config.fusion_endpoint on the
// AgentApp row. The backend resolves the Fusion bridge per call with this
// priority: agent tool_config.fusion_endpoint → FUSION360_HOST env →
// host.docker.internal:9876. This section is only rendered for agents that
// explicitly carry fusion360_* tools (e.g. the CAD Agent), so a blank value
// always means "use the platform default".
export default function FusionBridgeSection({ form, update }) {
  const toolConfig = form && typeof form.tool_config === 'object' ? form.tool_config : {};
  const value = toolConfig.fusion_endpoint || '';

  function setEndpoint(next) {
    const trimmed = (next || '').trim();
    const nextTc = { ...toolConfig };
    if (trimmed) nextTc.fusion_endpoint = trimmed;
    else delete nextTc.fusion_endpoint; // blank → remove override → default
    update({ tool_config: nextTc });
  }

  return (
    <Section
      title="Fusion 360 bridge"
      desc="Where this agent reaches your Fusion 360 add-in. Leave blank for the platform default."
      icon={Box}
    >
      <Field
        label="Fusion 360 endpoint"
        hint="Format: host or host:port (e.g. 192.168.1.50:9876). Leave blank for the default host.docker.internal:9876. Note: the backend runs in Docker, so 127.0.0.1 is the container itself — use host.docker.internal to reach Fusion on this Mac."
      >
        <input
          className={inputCls}
          value={value}
          onChange={(e) => setEndpoint(e.target.value)}
          placeholder="host.docker.internal:9876"
          spellCheck={false}
          autoComplete="off"
        />
      </Field>
    </Section>
  );
}
