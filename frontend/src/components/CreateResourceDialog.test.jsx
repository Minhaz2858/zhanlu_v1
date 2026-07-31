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
  base44: { entities: { Project: { filter: vi.fn().mockResolvedValue([]) },
                        KnowledgeBase: { filter: vi.fn().mockResolvedValue([]) } } },
}));
vi.mock('@/components/automation/SchedulePicker', () => ({ default: () => null }));

const { default: CreateResourceDialog } = await import('@/components/CreateResourceDialog');

describe('CreateResourceDialog output format', () => {
  beforeEach(() => navigateMock.mockClear());

  it('renders the format select with HTML report as the default', () => {
    render(<CreateResourceDialog open onOpenChange={() => {}} resourceType="automation" />);
    const trigger = screen.getByTestId('output-format-select');
    expect(trigger).toBeTruthy();
    expect(trigger.textContent).toContain('HTML report');
  });

  it('emits the output format into the prefill (raw value in parentheses)', async () => {
    render(<CreateResourceDialog open onOpenChange={() => {}} resourceType="automation" />);
    fireEvent.change(screen.getByPlaceholderText(/weekly sales report/i), {
      target: { value: 'Sales deck' },
    });
    fireEvent.click(screen.getByRole('button', { name: /create/i }));
    await waitFor(() => expect(navigateMock).toHaveBeenCalled());
    const url = navigateMock.mock.calls[0][0];
    const prefill = decodeURIComponent(new URLSearchParams(url.split('?')[1]).get('prefill'));
    expect(prefill).toContain('Output format');
    expect(prefill).toContain('(html)');
    expect(prefill).toContain('Sales deck');
  });
});
