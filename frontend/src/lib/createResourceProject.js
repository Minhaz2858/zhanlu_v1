// Resolve the `project` string stamped onto a chat-created resource.
//
// Bug fixed 2026-07-29: Chat.jsx built the create payload as
//   { ...parsedFields, session_id, project: pendingProject || ungrouped }
// so `project:` came AFTER the spread and OVERRODE the LLM-parsed
// `fields.project`. The create-dialog reliably fills that field from the
// "- Project：" prefill line (and the system prompt tells the LLM to
// echo it into create_resource.fields.project), but when no project chip was
// set in the chat (pendingProject empty) the task was stamped "Ungrouped"
// even though the user explicitly picked a project in the dialog.
//
// Correct precedence: explicit (parsed from the user's request) > contextual
// (the chat's project chip) > default (Ungrouped). Sentinel values the LLM
// may echo back for "no project" (ungrouped / global / 未分组 / 默认) are
// treated as absent so they don't shadow the chip.

const NO_PROJECT_SENTINELS = new Set(['ungrouped', 'global', 'default', 'none', '未分组', '默认', '无']);

function _real(value) {
  const v = (value == null ? '' : String(value)).trim();
  if (!v) return null;
  if (NO_PROJECT_SENTINELS.has(v.toLowerCase())) return null;
  return v;
}

// parsedFields: the LLM's create_resource.fields (may contain project).
// pendingProject: the chat's currently-selected project chip (may be null).
// ungroupedLabel: the translated "Ungrouped" label used as the final default.
export function pickCreateProject(parsedFields, pendingProject, ungroupedLabel) {
  const parsed = _real(parsedFields && parsedFields.project);
  if (parsed) return parsed;
  const chip = _real(pendingProject);
  if (chip) return chip;
  return ungroupedLabel;
}
