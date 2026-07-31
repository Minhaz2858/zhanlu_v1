import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { LanguageProvider } from '@/lib/LanguageProvider';
import RunFailureActions from './RunFailureActions';

beforeEach(() => {
  localStorage.setItem('zhanlu_lang', 'en');
});

afterEach(() => cleanup());

function renderWithRouter(ui, initialPath = '/') {
  return render(
    <LanguageProvider>
      <MemoryRouter initialEntries={[initialPath]}>
        <Routes>
          <Route path="*" element={ui} />
        </Routes>
      </MemoryRouter>
    </LanguageProvider>,
  );
}

describe('RunFailureActions', () => {
  it('renders the failure error and reason label', () => {
    renderWithRouter(
      <RunFailureActions
        error="Quota exceeded (429)"
        reason="quota"
      />,
    );
    expect(screen.getByText(/Quota exceeded/)).toBeTruthy();
    expect(screen.getByText(/Run was stopped because the upstream quota/)).toBeTruthy();
  });

  it('navigates to the cost settings tab on credit/quota', () => {
    renderWithRouter(
      <RunFailureActions error="Rate limit" reason="quota" />,
      '/some/other/page',
    );
    const link = screen.getByRole('link', { name: /Open cost settings/i });
    expect(link.getAttribute('href')).toBe('/settings#cost');
  });

  it('links to docs for paused / approval reasons', () => {
    renderWithRouter(
      <RunFailureActions error="Awaiting approval" reason="approval" />,
    );
    const link = screen.getByRole('link', { name: /Open the run history/i });
    expect(link.getAttribute('href')).toBe('/automation');
  });

  it('falls back to a generic support link when the reason is unknown', () => {
    renderWithRouter(
      <RunFailureActions error="Boom" reason={null} />,
    );
    expect(screen.getByRole('link', { name: /View run history/i })).toBeTruthy();
  });
});
