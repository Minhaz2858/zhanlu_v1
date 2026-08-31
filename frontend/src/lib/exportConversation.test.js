/**
 * Unit tests for the conversation markdown exporter
 * (frontend/src/lib/exportConversation.js).
 */
import { describe, it, expect } from 'vitest';
import { buildConversationMarkdown, sanitizeFilename } from './exportConversation';

const session = {
  title: 'Quarterly Report',
  agent_name: 'general_assistant',
  created_date: '2026-08-31T09:00:00Z',
};

const messages = [
  { role: 'user', content: 'Summarize Q3 sales', attachments: [{ name: 'sales.csv' }] },
  { role: 'assistant', content: 'Q3 sales grew 12%.' },
];

describe('buildConversationMarkdown', () => {
  it('includes the title as the H1', () => {
    expect(buildConversationMarkdown(session, messages)).toContain('# Quarterly Report');
  });

  it('includes agent name and date metadata', () => {
    const md = buildConversationMarkdown(session, messages);
    expect(md).toContain('**Agent:** general_assistant');
    expect(md).toContain('**Date:**');
  });

  it('labels user and assistant roles', () => {
    const md = buildConversationMarkdown(session, messages);
    expect(md).toContain('## 👤 User');
    expect(md).toContain('## 🤖 Assistant');
  });

  it('includes message content verbatim', () => {
    const md = buildConversationMarkdown(session, messages);
    expect(md).toContain('Q3 sales grew 12%.');
    expect(md).toContain('Summarize Q3 sales');
  });

  it('lists attachments as quoted lines', () => {
    expect(buildConversationMarkdown(session, messages)).toContain('> 📎 sales.csv');
  });

  it('handles empty messages and missing session gracefully', () => {
    const md = buildConversationMarkdown(null, []);
    expect(md).toContain('# Conversation');
    expect(md).not.toContain('## 👤');
  });

  it('tolerates messages without content', () => {
    const md = buildConversationMarkdown(session, [{ role: 'tool', content: '' }]);
    expect(md).toContain('## 🔧 Tool');
  });
});

describe('sanitizeFilename', () => {
  it('strips filesystem-hostile characters', () => {
    expect(sanitizeFilename('a/b:c*?"<>|d')).toBe('a_b_c______d');
  });

  it('caps at 60 chars and falls back to conversation', () => {
    expect(sanitizeFilename('x'.repeat(200)).length).toBe(60);
    expect(sanitizeFilename('')).toBe('conversation');
  });
});
