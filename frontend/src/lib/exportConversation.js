/**
 * Conversation markdown export — pure builder + download helper.
 * Client-side only (no backend): fetches are done by the caller, this
 * module only turns (session, messages) into a downloadable .md file.
 */

const ROLE_LABELS = {
  user: '👤 User',
  assistant: '🤖 Assistant',
  system: '⚙️ System',
  tool: '🔧 Tool',
};

export function buildConversationMarkdown(session, messages) {
  const s = session || {};
  const lines = [];
  lines.push(`# ${s.title || 'Conversation'}`);
  if (s.agent_name) lines.push(`\n**Agent:** ${s.agent_name}`);
  if (s.created_date) {
    try {
      lines.push(`**Date:** ${new Date(s.created_date).toLocaleString()}`);
    } catch {
      /* non-parseable date — skip */
    }
  }
  lines.push('');
  lines.push('---');
  lines.push('');

  for (const m of messages || []) {
    const role = ROLE_LABELS[m.role] || `⚙️ ${m.role || 'unknown'}`;
    lines.push(`## ${role}`);
    lines.push('');
    if (m.attachments && m.attachments.length) {
      for (const a of m.attachments) {
        lines.push(`> 📎 ${a.name || a.file_url || 'attachment'}`);
      }
      lines.push('');
    }
    lines.push(m.content || '');
    lines.push('');
  }
  return lines.join('\n');
}

export function sanitizeFilename(title) {
  return (title || 'conversation').replace(/[\\/:*?"<>|]/g, '_').slice(0, 60);
}

export function downloadConversationMarkdown(session, messages) {
  const md = buildConversationMarkdown(session, messages);
  const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${sanitizeFilename(session?.title)}.md`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
