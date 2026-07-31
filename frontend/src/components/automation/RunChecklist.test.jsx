import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { LanguageProvider } from '@/lib/LanguageProvider';
import RunChecklist from './RunChecklist';

afterEach(() => cleanup());

const baseSteps = [
  { number: 1, description: 'Gather source data', status: 'done' },
  { number: 2, description: 'Compute summary', status: 'done' },
  { number: 3, description: 'Render report', status: 'running' },
  { number: 4, description: 'Deliver to chat', status: 'pending' },
];

function renderWithProvider(ui) {
  return render(<LanguageProvider>{ui}</LanguageProvider>);
}

describe('RunChecklist', () => {
  it('renders every step as a checklist item with role="listitem"', () => {
    renderWithProvider(<RunChecklist steps={baseSteps} status="running" />);
    expect(screen.getAllByRole('listitem')).toHaveLength(4);
    expect(screen.getByText('Gather source data')).toBeTruthy();
    expect(screen.getByText('Render report')).toBeTruthy();
  });

  it('exposes an accessible ordered list with descriptive label', () => {
    renderWithProvider(<RunChecklist steps={baseSteps} status="running" />);
    const list = screen.getByRole('list');
    expect(list.tagName.toLowerCase()).toBe('ol');
  });

  it('shows a warning row with a visible warning icon when a step is warning', () => {
    const steps = [
      { number: 1, description: 'Look up API key', status: 'warning', detail: 'Using last successful key' },
      { number: 2, description: 'Continue anyway', status: 'done' },
    ];
    renderWithProvider(<RunChecklist steps={steps} status="running" />);
    expect(screen.getByLabelText(/Warning on step 1/i)).toBeTruthy();
    expect(screen.getByText(/Using last successful key/)).toBeTruthy();
  });

  it('renders a progress bar bound to the completed-step ratio', () => {
    renderWithProvider(<RunChecklist steps={baseSteps} status="running" />);
    const progress = screen.getByRole('progressbar');
    expect(progress.getAttribute('aria-valuenow')).toBe('50');
    expect(progress.getAttribute('aria-valuemax')).toBe('100');
  });

  it('marks failed steps as failure with an accessible label', () => {
    const steps = [
      { number: 1, description: 'Try upstream', status: 'failed', detail: '429 Too Many Requests' },
    ];
    renderWithProvider(<RunChecklist steps={steps} status="failed" />);
    expect(screen.getByLabelText(/Failure on step 1/i)).toBeTruthy();
    expect(screen.getByText(/429 Too Many Requests/)).toBeTruthy();
  });
});
