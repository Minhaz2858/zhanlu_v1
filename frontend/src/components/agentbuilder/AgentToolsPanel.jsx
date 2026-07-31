/**
 * AgentToolsPanel — shows the user exactly which tools their newly created
 * (or edited) agent will have. Two sections:
 *
 *   1. "From your selections" — the skill-mapped tools (Web Search -> web_search, etc.)
 *   2. "Baseline tools (always on)" — DEFAULT_USER_AGENT_TOOLS, minus any duplicates
 *
 * The data helpers (resolveTools, DEFAULT_USER_AGENT_TOOLS, SKILL_DISPLAY_TO_TOOL)
 * live in agentTools.js so they can be unit-tested under vitest's node env
 * without pulling in shadcn/window-dependent imports.
 */
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Wrench, Plus, ShieldCheck } from 'lucide-react';

import { resolveTools, DEFAULT_USER_AGENT_TOOLS } from './agentTools';

export default function AgentToolsPanel({ agent, onAddTools }) {
  const { mapped, baseline } = resolveTools(agent);
  const displayName = (agent && agent.name) || 'this agent';

  return (
    <Card data-testid="agent-tools-panel" className="w-full">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Wrench className="h-4 w-4" /> Tools for {displayName}
        </CardTitle>
        <CardDescription>
          These are the tools the agent will be able to call. Add more to
          expand what it can do.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div>
          <h4 className="text-sm font-medium mb-2">From your selections</h4>
          {mapped.length === 0 ? (
            <p className="text-sm text-muted-foreground">No skills selected.</p>
          ) : (
            <ul className="space-y-1" data-testid="agent-tools-mapped">
              {mapped.map((tool) => (
                <li
                  key={tool}
                  className="text-sm font-mono px-2 py-1 bg-muted rounded"
                >
                  {tool}
                </li>
              ))}
            </ul>
          )}
        </div>

        <div>
          <h4 className="text-sm font-medium mb-2 flex items-center gap-2">
            <ShieldCheck className="h-4 w-4" /> Baseline tools (always on)
          </h4>
          <ul className="space-y-1" data-testid="agent-tools-baseline">
            {baseline.map((tool) => (
              <li
                key={tool}
                className="text-sm font-mono px-2 py-1 bg-muted/50 rounded"
              >
                {tool}
              </li>
            ))}
          </ul>
          <p className="text-xs text-muted-foreground mt-2">
            {DEFAULT_USER_AGENT_TOOLS.join(', ')} are safe-by-default. They
            let the agent look things up, persist context, and plan
            multi-step work without requiring extra consent.
          </p>
        </div>

        <Button onClick={onAddTools} variant="outline" className="w-full">
          <Plus className="h-4 w-4 mr-2" /> Add more tools
        </Button>
      </CardContent>
    </Card>
  );
}
