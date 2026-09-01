// launchMarketAgent — turn a MarketAgent (catalog entry) into a live chat.
//
// A market card (MarketAgent) is a catalog listing; the thing you actually chat
// with is an AgentApp. This helper finds-or-creates the user's AgentApp for a
// market agent and returns its id, so a "Use" button can hand it straight to
// the chat page via `navigate('/?agent=<id>')` (Chat.jsx pre-selects `?agent=`).
import { base44 } from '@/api/base44Client';
import { generateAgentPrompts, recommendSkills } from '@/lib/generateAgentPrompts';

// Fusion 360 toolset — kept in sync with scripts/create_cad_agent.py (backend).
// The generic market "Clone" flow does NOT copy tool_config, so a cloned CAD
// Agent would silently lose its Fusion tools; we restore them here.
// NOTE: this list MUST stay in sync with create_cad_agent.py — if a granular
// tool is added there but not here, a "Use" click strips it from the agent.
export const CAD_TOOL_CONFIG = {
  enabled_tools: [
    'fusion360_clear',
    'fusion360_sketch_create',
    'fusion360_sketch_circle',
    'fusion360_sketch_rectangle',
    'fusion360_sketch_polygon',
    'fusion360_extrude',
    'fusion360_fillet',
    'fusion360_mirror',
    'fusion360_thread',
    'fusion360_info',
    'fusion360_probe',
    'fusion360_verify_build',
    'fusion360_revolve',
    'fusion360_coil',
    'fusion360_user_parameter',
    'fusion360_circular_pattern',
    'fusion360_lookup_api',
    'fusion360_execute_python',
    'fusion360_ping',
    'fusion360_export_geometry',
    'fusion360_import_dxf',
    'fusion360_make_drawing',
    'fusion360_save',
    'fusion360_project',
    'fusion360_box',
    'fusion360_cylinder',
    'fusion360_sphere',
    'fusion360_torus',
    'fusion360_sweep',
    'fusion360_loft',
    'fusion360_shell',
    'fusion360_chamfer',
    'fusion360_edge_chamfer',
    'fusion360_extend_face',
    'fusion360_hole',
    'fusion360_rectangular_pattern',
    'fusion360_combine',
    'fusion360_construction_plane',
    'fusion360_move',
    'fusion360_sketch_line',
    'fusion360_sketch_arc',
    'fusion360_sketch_arc_3point',
    'fusion360_sketch_spline',
    'fusion360_component',
    'fusion360_slider_joint',
    'fusion360_revolute_joint',
    'fusion360_rigid_joint',
    'fusion360_joint_limits',
    'fusion360_physical_properties',
    'fusion360_measure',
    'todo',
    'execute_code',
    'read_file',
    'write_file',
    'memory',
    'create_artifact',
  ],
  // fusion_endpoint: optional "host" or "host:port" override for the Fusion 360
  // bridge. When absent/empty the backend falls back to FUSION360_HOST env ->
  // host.docker.internal:9876. Set per user/agent to point at a different Fusion.
};

/**
 * Find-or-create the functional AgentApp for a market agent.
 * @returns {Promise<{ id: string, created: boolean }>}
 */
export async function ensureAgentApp(agent) {
  if (!agent || !agent.name) throw new Error('agent is required');

  let id = null;
  let created = false;

  // 1. Reuse an existing copy (e.g. a seeded company-visible CAD Agent).
  let existingAgent = null;
  try {
    const existing = await base44.entities.AgentApp.filter({ name: agent.name });
    if (Array.isArray(existing) && existing.length > 0) {
      existingAgent = existing[0];
      id = existingAgent.id;
    }
  } catch { /* fall through to create */ }

  // 2. Create a fresh copy if none exists.
  if (!id) {
    let skills = [];
    try {
      const tools = await base44.entities.Tool.list();
      skills = recommendSkills(agent, tools);
    } catch { /* skills are optional */ }

    const createdAgent = await base44.entities.AgentApp.create({
      name: agent.name,
      description: agent.description,
      capabilities: agent.capabilities || [],
      model: 'automatic',
      status: 'active',
      data_read: true,
      agent_type: 'sequential',
      topology: 'standalone',
      skills,
      ...generateAgentPrompts(agent),
    });
    id = createdAgent.id;
    created = true;
  }

  // 3. The CAD Agent must always carry its Fusion toolset — both fresh clones
  //    and stale copies from before this wiring lack it. Idempotent. Preserve
  //    any per-user fusion_endpoint so a "Use" click doesn't clobber it.
  if (agent.name === 'CAD Agent') {
    try {
      const prior = (existingAgent && existingAgent.tool_config) || {};
      const config = { ...CAD_TOOL_CONFIG };
      if (prior.fusion_endpoint) config.fusion_endpoint = prior.fusion_endpoint;
      await base44.entities.AgentApp.update(id, { tool_config: config });
    } catch { /* best-effort — the seeded copy already has it */ }
  }

  return { id, created };
}
