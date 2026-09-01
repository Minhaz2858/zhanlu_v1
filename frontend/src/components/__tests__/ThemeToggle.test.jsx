import { describe, it, expect, vi, beforeEach } from 'vitest';
import { useState } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';

const setTheme = vi.fn();
vi.mock('@/lib/useTheme', () => ({
  useTheme: () => {
    const [theme, setT] = useState('system');
    const setThemeFn = (t) => { setT(t); setTheme(t); };
    return { theme, setTheme: setThemeFn };
  },
}));

import ThemeToggle from '../ThemeToggle';

describe('ThemeToggle', () => {
  beforeEach(() => { setTheme.mockReset(); });

  it('renders a button with an accessible label', () => {
    render(<ThemeToggle />);
    expect(screen.getByRole('button', { name: /theme/i })).toBeInTheDocument();
  });

  it('cycles system -> light -> dark -> system', () => {
    render(<ThemeToggle />);
    const btn = screen.getByRole('button', { name: /theme/i });
    fireEvent.click(btn);
    expect(setTheme).toHaveBeenLastCalledWith('light');
    fireEvent.click(btn);
    expect(setTheme).toHaveBeenLastCalledWith('dark');
    fireEvent.click(btn);
    expect(setTheme).toHaveBeenLastCalledWith('system');
  });
});
