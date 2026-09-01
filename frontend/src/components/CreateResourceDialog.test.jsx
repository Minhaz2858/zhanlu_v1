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
        refreshCadence: 'Refresh interval', dashboardHint: 'dashboard hint',
        resourceLabels: { automation: 'Automation Task', agent: 'Agent', kb: 'Knowledge Base', dashboard: 'Dashboard' },
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
// The automation branch now renders the shared skills picker; stub it out so
// these output-format tests stay focused (skills are covered separately).
vi.mock('@/components/agent/SkillsSection', () => ({ default: () => null }));

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

describe('CreateResourceDialog dashboard', () => {
  beforeEach(() => navigateMock.mockClear());

  it('renders dashboard templates and the refresh cadence select', () => {
    render(<CreateResourceDialog open onOpenChange={() => {}} resourceType="dashboard" />);
    expect(screen.getByText('Sales Performance Dashboard')).toBeTruthy();
    expect(screen.getByTestId('dashboard-refresh-select')).toBeTruthy();
    // Default refresh cadence is 30s
    expect(screen.getByTestId('dashboard-refresh-select').textContent).toContain('30s');
  });

  it('emits the FULLSTACK_REALTIME intent chip into the prefill', async () => {
    render(<CreateResourceDialog open onOpenChange={() => {}} resourceType="dashboard" />);
    fireEvent.change(screen.getByPlaceholderText(/weekly sales report/i), {
      target: { value: 'Ops KPI' },
    });
    fireEvent.click(screen.getByRole('button', { name: /create/i }));
    await waitFor(() => expect(navigateMock).toHaveBeenCalled());
    const url = navigateMock.mock.calls[0][0];
    const prefill = decodeURIComponent(new URLSearchParams(url.split('?')[1]).get('prefill'));
    // The literal "dashboard" keyword deterministically routes the resolver
    // to the dashboard-generation skill (never the static-HTML fallback).
    expect(prefill).toContain('FULL-STACK REALTIME DASHBOARD');
    expect(prefill).toContain('create_fullstack_dashboard');
    expect(prefill).toContain('FULLSTACK_REALTIME');
    expect(prefill).toContain('Refresh interval');
    expect(prefill).toContain('30s');
    expect(prefill).toContain('Ops KPI');
  });

  it('applies a template and emits its name + description', async () => {
    render(<CreateResourceDialog open onOpenChange={() => {}} resourceType="dashboard" />);
    fireEvent.click(screen.getByText('Equipment OEE Monitor'));
    fireEvent.click(screen.getByRole('button', { name: /create/i }));
    await waitFor(() => expect(navigateMock).toHaveBeenCalled());
    const url = navigateMock.mock.calls[0][0];
    const prefill = decodeURIComponent(new URLSearchParams(url.split('?')[1]).get('prefill'));
    expect(prefill).toContain('Equipment OEE Monitor');
    expect(prefill).toContain('overall effectiveness');
  });
});
