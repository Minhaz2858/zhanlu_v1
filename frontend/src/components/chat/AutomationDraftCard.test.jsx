import { describe, it, expect, vi } from 'vitest';

vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({
    lang: 'en',
    t: new Proxy({}, { get: (o, k) => (o[k] ??= new Proxy({}, { get: () => '' })) }),
  }),
}));
vi.mock('@/components/automation/SchedulePicker', () => ({ default: () => null }));

const { parsePrefill } = await import('@/components/chat/AutomationDraftCard');

describe('AutomationDraftCard parsePrefill output format', () => {
  it('extracts the raw format value from the prefill line', () => {
    const prefill = [
      'Create a new Automation Task:',
      '- Name: Sales deck',
      '- Type: report_generation',
      '- Output format：Word document (docx)',
      '- Description: weekly deck',
    ].join('\n');
    const parsed = parsePrefill(prefill, { createDialog: {} }, true);
    expect(parsed.outputFormat).toBe('docx');
  });

  it('defaults to html when the line is absent', () => {
    const parsed = parsePrefill('Create a new Automation Task:\n- Name: X', { createDialog: {} }, true);
    expect(parsed.outputFormat).toBe('html');
  });
});
