import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const navigateMock = vi.fn();
vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
}));
vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({
    lang: 'en',
    t: {
      createDialog: {
        name: 'Name', namePh: 'e.g. Weekly sales report',
        schedule: 'Schedule', description: 'Description', descPh: 'Describe the task',
        type: 'Type', typePh: 'Select type',
        project: 'Project', projectPh: 'Select project', globalProject: 'Global',
        templatesLabel: 'Templates', cancel: 'Cancel', submit: 'Create',
        desc: 'desc', capabilities: 'Capabilities', capabilitiesPh: 'caps',
        outputFormat: 'Output format',
        resourceLabels: { automation: 'Automation Task', agent: 'Agent', kb: 'Knowledge Base' },
      },
      agentBuilder: { buildButton: 'Build' },
      automation: { types: { report_generation: 'Report', data_sync: 'Data sync' } },
      detail: { kbTypes: {} },
    },
  }),
}));
vi.mock('@/api/base44Client', () => ({
  base44: {
    entities: {
      Project: { filter: vi.fn().mockResolvedValue([]) },
      KnowledgeBase: { filter: vi.fn().mockResolvedValue([]) },
    },
  },
}));
vi.mock('@/components/automation/SchedulePicker', () => ({ default: () => null }));

// Controlled stub: clicking it pushes a fixed skill list through the
// SkillsSection `update` callback, exactly like the real picker would.
vi.mock('@/components/agent/SkillsSection', () => ({
  default: ({ update }) => (
    <button
      type="button"
      data-testid="skills-stub"
      onClick={() => update({ skills: ['weekly_report', 'erp_writeback'] })}
    >
      stub-add-skills
    </button>
  ),
}));

const { default: CreateResourceDialog } = await import('@/components/CreateResourceDialog');

describe('CreateResourceDialog skills picker', () => {
  beforeEach(() => navigateMock.mockClear());

  it('emits selected skills into the prefill as a Skills line', async () => {
    render(<CreateResourceDialog open onOpenChange={() => {}} resourceType="automation" />);
    // The skills section only renders for the automation branch.
    expect(screen.getByTestId('skills-stub')).toBeTruthy();

    fireEvent.change(screen.getByPlaceholderText(/weekly sales report/i), {
      target: { value: 'Sales deck' },
    });
    fireEvent.click(screen.getByTestId('skills-stub'));
    fireEvent.click(screen.getByRole('button', { name: /create/i }));

    await waitFor(() => expect(navigateMock).toHaveBeenCalled());
    const url = navigateMock.mock.calls[0][0];
    const prefill = decodeURIComponent(new URLSearchParams(url.split('?')[1]).get('prefill'));
    expect(prefill).toContain('Skills');
    expect(prefill).toContain('weekly_report, erp_writeback');
  });
});
