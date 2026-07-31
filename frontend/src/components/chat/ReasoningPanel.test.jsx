/**
 * ReasoningPanel tests — collapsible model reasoning block.
 *
 * ReasoningPanel is the single source of truth for the "REASONING
 * (N CHARS)" UI shared by:
 *   - ActivitySteps (testId="activity-steps-reasoning")
 *   - SkillMessageBubble (testId="skill-reasoning-panel")
 *   - BuilderMessageBubble (testId="builder-reasoning-panel")
 *
 * The component must:
 *   - Render nothing for empty / whitespace / non-string inputs
 *     (callers don't have to gate).
 *   - Show the char count + a Brain icon in the summary line.
 *   - Toggle open/closed on click.
 *   - Switch language copy (English vs Chinese) via LanguageProvider.
 *   - Accept an optional className (used by ActivitySteps to add a
 *     top border / extra spacing when nesting inside the step list).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

// Mock useLanguage. `vi.hoisted` shares the same vi.fn() instance
// between the mock factory (which is hoisted to the top) and the test
// bodies (which need to override its return value).
const useLanguageMock = vi.hoisted(() =>
  vi.fn(() => ({ lang: 'zh', setLang: () => {}, t: {} }))
);

vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: useLanguageMock,
}));

import ReasoningPanel from './ReasoningPanel';

beforeEach(() => {
  try { localStorage.removeItem('zhanlu_lang'); } catch {}
});

// ---------------------------------------------------------------------------
// Empty / null guards
// ---------------------------------------------------------------------------

describe('ReasoningPanel — empty / null guards', () => {
  it('renders nothing when reasoning is undefined', () => {
    const { container } = render(<ReasoningPanel />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when reasoning is null', () => {
    const { container } = render(<ReasoningPanel reasoning={null} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when reasoning is the empty string', () => {
    const { container } = render(<ReasoningPanel reasoning="" />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when reasoning is whitespace-only', () => {
    // NOTE: pass the whitespace via a JS expression (`{}`) rather than a
    // bare JSX attribute string. JSX attribute strings are literal —
    // ``"\n"`` inside them is two characters (backslash + n), not a
    // newline — so the whitespace wouldn't actually be whitespace and
    // the guard would not fire. Wrapping in ``{}`` re-enables the JS
    // string-literal escape rules.
    const whitespace = '   \n\t  ';
    const { container } = render(<ReasoningPanel reasoning={whitespace} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when reasoning is a non-string type', () => {
    const { container } = render(<ReasoningPanel reasoning={42} />);
    expect(container.firstChild).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Summary line: copy + char count + Brain icon
// ---------------------------------------------------------------------------

describe('ReasoningPanel — summary line', () => {
  const REASONING = 'the model thought about it for a while';

  it('renders the char count of the reasoning text', () => {
    render(<ReasoningPanel reasoning={REASONING} testId="rp" />);
    const panel = screen.getByTestId('rp');
    expect(panel.textContent).toMatch(String(REASONING.length));
  });

  it('renders the reasoning text inside a <pre>', () => {
    render(<ReasoningPanel reasoning={REASONING} testId="rp" />);
    const panel = screen.getByTestId('rp');
    const pre = panel.querySelector('pre');
    expect(pre).not.toBeNull();
    expect(pre.textContent).toBe(REASONING);
  });

  it('applies the default testId when none is passed', () => {
    render(<ReasoningPanel reasoning={REASONING} />);
    expect(screen.getByTestId('reasoning-panel')).toBeInTheDocument();
  });

  it('honors a custom testId', () => {
    render(
      <ReasoningPanel reasoning={REASONING} testId="custom-id" />
    );
    expect(screen.getByTestId('custom-id')).toBeInTheDocument();
    expect(screen.queryByTestId('reasoning-panel')).toBeNull();
  });

  it('passes className through to the outer <details>', () => {
    render(
      <ReasoningPanel
        reasoning={REASONING}
        className="mt-3 border-t pt-1.5"
        testId="rp"
      />
    );
    const panel = screen.getByTestId('rp');
    expect(panel.className).toMatch(/mt-3/);
    expect(panel.className).toMatch(/border-t/);
    expect(panel.className).toMatch(/pt-1\.5/);
  });
});

// ---------------------------------------------------------------------------
// Language copy
// ---------------------------------------------------------------------------

describe('ReasoningPanel — language', () => {
  const REASONING = 'thinking';

  it('renders Chinese copy by default', () => {
    // Default lang is 'zh' (set in beforeEach via localStorage removal).
    render(<ReasoningPanel reasoning={REASONING} testId="rp" />);
    const panel = screen.getByTestId('rp');
    expect(panel.textContent).toMatch(/推理/);
    expect(panel.textContent).toMatch(/字/);
  });

  it('renders English copy when the user has English selected', () => {
    useLanguageMock.mockReturnValueOnce({ lang: 'en', setLang: () => {}, t: {} });
    render(<ReasoningPanel reasoning={REASONING} testId="rp" />);
    const panel = screen.getByTestId('rp');
    expect(panel.textContent).toMatch(/Reasoning/);
    expect(panel.textContent).toMatch(/chars/);
    expect(panel.textContent).not.toMatch(/推理/);
  });
});

// ---------------------------------------------------------------------------
// Toggle behavior
// ---------------------------------------------------------------------------

describe('ReasoningPanel — toggle', () => {
  const REASONING = 'the actual reasoning content';

  it('starts closed — the <pre> is inside the <details> but not displayed', () => {
    // <details> renders its children regardless of open state, but the
    // <pre> is only *visually* shown when the details is open. The user
    // toggles by clicking the <summary>.
    render(<ReasoningPanel reasoning={REASONING} testId="rp" />);
    const details = screen.getByTestId('rp');
    expect(details.open).toBe(false);
  });

  it('opens when the user clicks the summary', () => {
    render(<ReasoningPanel reasoning={REASONING} testId="rp" />);
    const details = screen.getByTestId('rp');
    const summary = details.querySelector('summary');
    expect(summary).not.toBeNull();

    fireEvent.click(summary);

    expect(details.open).toBe(true);
  });

  it('closes again on a second click', () => {
    render(<ReasoningPanel reasoning={REASONING} testId="rp" />);
    const details = screen.getByTestId('rp');
    const summary = details.querySelector('summary');

    fireEvent.click(summary);
    expect(details.open).toBe(true);

    fireEvent.click(summary);
    expect(details.open).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Visual marker: Brain icon so the panel reads as "model thinking"
// rather than as another trace step
// ---------------------------------------------------------------------------

describe('ReasoningPanel — visual marker', () => {
  it('renders an svg in the summary line (the Brain icon)', () => {
    const { container } = render(
      <ReasoningPanel reasoning="x" testId="rp" />
    );
    const summary = container.querySelector('summary');
    // lucide-react renders icons as <svg>
    const svg = summary?.querySelector('svg');
    expect(svg).not.toBeNull();
  });
});