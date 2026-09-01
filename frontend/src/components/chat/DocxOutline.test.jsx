import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import DocxOutline from './DocxOutline';

const outline = [
  { level: 1, text: 'Executive Summary', id: 'executive-summary' },
  { level: 2, text: 'Goals',             id: 'goals' },
  { level: 1, text: 'Method',            id: 'method' },
];

describe('DocxOutline', () => {
  it('renders nothing when outline is empty', () => {
    const { container } = render(<DocxOutline outline={[]} onJump={() => {}} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders heading text in document order', () => {
    render(<DocxOutline outline={outline} onJump={() => {}} />);
    const items = screen.getAllByRole('button');
    expect(items.map((b) => b.textContent)).toEqual([
      'Executive Summary', 'Goals', 'Method',
    ]);
  });

  it('indents nested headings more than top-level headings', () => {
    render(<DocxOutline outline={outline} onJump={() => {}} />);
    const summary = screen.getByText('Executive Summary').closest('button');
    const goals = screen.getByText('Goals').closest('button');
    // Level 2 (Goals) should have a larger left padding than level 1 (Summary)
    const summaryPadding = summary.className.match(/pl-(\d+)/);
    const goalsPadding = goals.className.match(/pl-(\d+)/);
    expect(summaryPadding).toBeTruthy();
    expect(goalsPadding).toBeTruthy();
    expect(parseInt(goalsPadding[1])).toBeGreaterThan(parseInt(summaryPadding[1]));
  });

  it('fires onJump with the heading id on click', () => {
    const onJump = vi.fn();
    render(<DocxOutline outline={outline} onJump={onJump} />);
    fireEvent.click(screen.getByText('Method'));
    expect(onJump).toHaveBeenCalledWith('method');
  });
});
