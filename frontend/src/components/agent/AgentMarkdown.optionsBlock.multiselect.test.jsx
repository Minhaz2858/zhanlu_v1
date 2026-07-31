/**
 * Regression: OptionsBlock supports multi-select (2026-07-28).
 *
 * Spec: docs/superpowers/specs/2026-07-28-skill-agent-multi-select-options-design.md §3.3.1
 *
 * The OptionsBlock component is rendered by AgentMarkdown for any
 * `:::options` markdown block. With the new `multiSelect` prop:
 *   - false (default): single-click UX (back-compat for Agent Builder / Embedded Agent Builder)
 *   - true: multi-select with toggle chips and a "Use these (N)" commit button
 *
 * This test file uses a custom render harness built on React 18's
 * createRoot (already in the project's deps) to avoid pulling in
 * @testing-library/react, which is not currently in the project.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';

// Re-import AgentMarkdown to access OptionsBlock indirectly through
// its public API. We use AgentMarkdown.default with a string content
// containing a :::options block to trigger the OptionsBlock render.
import AgentMarkdown from './AgentMarkdown.jsx';

const SAMPLE = `Intro line.

:::options
Audience: Individual dev
Audience: Team lead
Format: Inline comments
Format: Structured report
Focus: Security (OWASP, injection, auth)
:::`;

let container = null;
let root = null;
const mounted = [];

function render(jsx) {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => { root.render(jsx); });
  mounted.push(root);
}

afterEach(() => {
  for (const r of mounted) {
    act(() => r.unmount());
  }
  mounted.length = 0;
  if (container && container.parentNode) container.parentNode.removeChild(container);
  container = null;
  root = null;
});

function getChips() {
  return Array.from(container.querySelectorAll('button[role="checkbox"]'));
}

function getCommitButton() {
  // The commit button has no role attribute, but it contains "Use these".
  return Array.from(container.querySelectorAll('button')).find((b) =>
    /Use these \(\d+\)/.test(b.textContent || '')
  );
}

describe('OptionsBlock (multiSelect={true})', () => {
  it('1. renders one chip per option line', () => {
    const onSelect = vi.fn();
    render(<AgentMarkdown multiSelect={true} onOptionSelect={onSelect}>{SAMPLE}</AgentMarkdown>);
    expect(getChips().length).toBe(5);
  });

  it('2. toggles selected on click (aria-pressed reflects state)', () => {
    const onSelect = vi.fn();
    render(<AgentMarkdown multiSelect={true} onOptionSelect={onSelect}>{SAMPLE}</AgentMarkdown>);
    const [a] = getChips();
    expect(a.getAttribute('aria-pressed')).toBe('false');
    act(() => a.click());
    expect(a.getAttribute('aria-pressed')).toBe('true');
    act(() => a.click());
    expect(a.getAttribute('aria-pressed')).toBe('false');
  });

  it('3. multi-select state: clicking A, B, C sets all three selected', () => {
    const onSelect = vi.fn();
    render(<AgentMarkdown multiSelect={true} onOptionSelect={onSelect}>{SAMPLE}</AgentMarkdown>);
    const [a, b, c] = getChips();
    act(() => a.click());
    act(() => b.click());
    act(() => c.click());
    expect(a.getAttribute('aria-pressed')).toBe('true');
    expect(b.getAttribute('aria-pressed')).toBe('true');
    expect(c.getAttribute('aria-pressed')).toBe('true');
  });

  it('4. commit button is disabled when 0 selected', () => {
    const onSelect = vi.fn();
    render(<AgentMarkdown multiSelect={true} onOptionSelect={onSelect}>{SAMPLE}</AgentMarkdown>);
    const btn = getCommitButton();
    expect(btn).toBeTruthy();
    expect(btn.textContent).toBe('Use these (0)');
    expect(btn.hasAttribute('disabled')).toBe(true);
  });

  it('5. commit button is enabled and label shows (N) when N>=1', () => {
    const onSelect = vi.fn();
    render(<AgentMarkdown multiSelect={true} onOptionSelect={onSelect}>{SAMPLE}</AgentMarkdown>);
    const [a, b] = getChips();
    act(() => a.click());
    act(() => b.click());
    const btn = getCommitButton();
    expect(btn.textContent).toBe('Use these (2)');
    expect(btn.hasAttribute('disabled')).toBe(false);
  });

  it('6. commit calls onOptionSelect with comma-separated text', () => {
    const onSelect = vi.fn();
    render(<AgentMarkdown multiSelect={true} onOptionSelect={onSelect}>{SAMPLE}</AgentMarkdown>);
    const [a, b, c] = getChips();
    act(() => a.click());
    act(() => b.click());
    act(() => c.click());
    act(() => getCommitButton().click());
    expect(onSelect).toHaveBeenCalledTimes(1);
    const arg = onSelect.mock.calls[0][0];
    expect(arg).toBe('Audience: Individual dev, Audience: Team lead, Format: Inline comments');
  });

  it('7. selected state resets after commit', () => {
    const onSelect = vi.fn();
    render(<AgentMarkdown multiSelect={true} onOptionSelect={onSelect}>{SAMPLE}</AgentMarkdown>);
    const [a] = getChips();
    act(() => a.click());
    act(() => getCommitButton().click());
    // After commit, all chips should be unselected.
    const chipsAfter = getChips();
    for (const c of chipsAfter) {
      expect(c.getAttribute('aria-pressed')).toBe('false');
    }
  });
});

describe('OptionsBlock (multiSelect={false} default) — back-compat', () => {
  it('8. clicking a chip immediately calls onOptionSelect with that chip text', () => {
    const onSelect = vi.fn();
    // No multiSelect prop at all — must default to false.
    render(<AgentMarkdown onOptionSelect={onSelect}>{SAMPLE}</AgentMarkdown>);
    // The chips in single-click mode do NOT have role="checkbox".
    const singleChips = Array.from(container.querySelectorAll('button')).filter(
      (b) => !/Use these/.test(b.textContent || '')
    );
    // Filter to the 5 option buttons (they're inside the :::options block).
    // In single-click mode, no commit button is rendered, so the total
    // button count should equal the option count.
    expect(singleChips.length).toBe(5);
    act(() => singleChips[2].click());
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect.mock.calls[0][0]).toBe('Format: Inline comments');
  });

  it('9. multiSelect={false} explicitly is the same as omitting the prop', () => {
    const onSelect1 = vi.fn();
    const onSelect2 = vi.fn();
    // Render once with multiSelect={false} explicitly.
    const a = document.createElement('div'); document.body.appendChild(a);
    const ra = createRoot(a);
    act(() => ra.render(<AgentMarkdown multiSelect={false} onOptionSelect={onSelect1}>{SAMPLE}</AgentMarkdown>));
    // Render once with the prop omitted.
    const b = document.createElement('div'); document.body.appendChild(b);
    const rb = createRoot(b);
    act(() => rb.render(<AgentMarkdown onOptionSelect={onSelect2}>{SAMPLE}</AgentMarkdown>));
    // Both should have 5 clickable option chips and NO commit button.
    const aChips = Array.from(a.querySelectorAll('button')).filter(
      (b) => !/Use these/.test(b.textContent || '')
    );
    const bChips = Array.from(b.querySelectorAll('button')).filter(
      (b) => !/Use these/.test(b.textContent || '')
    );
    expect(aChips.length).toBe(5);
    expect(bChips.length).toBe(5);
    expect(a.querySelector('[role="checkbox"]')).toBeNull();
    expect(b.querySelector('[role="checkbox"]')).toBeNull();
    act(() => ra.unmount());
    act(() => rb.unmount());
    document.body.removeChild(a);
    document.body.removeChild(b);
  });
});
