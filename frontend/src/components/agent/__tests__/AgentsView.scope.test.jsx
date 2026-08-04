import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import AgentsView from '@/components/agent/AgentsView';

vi.mock('@/api/base44Client', () => ({
  base44: { entities: { AgentApp: { list: vi.fn() } } },
}));

vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({ t: { common: { run: 'Run' }, mySpace: { empty: '', emptyHint: '', tabs: {} } }, lang: 'en' }),
}));

vi.mock('@/lib/systemAgents', () => ({
  filterUserAgents: (data) => data,
}));

const sample = [
  { id: '1', name: 'Mine', resource_type: 'personal' },
  { id: '2', name: 'Shared', resource_type: 'company', is_shared_with_me: true },
];

describe('AgentsView scope', () => {
  it('renders only personal agents when scope="personal"', async () => {
    const { base44 } = await import('@/api/base44Client');
    base44.entities.AgentApp.list.mockResolvedValue(sample);
    render(<MemoryRouter><AgentsView scope="personal" /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText('Mine')).toBeInTheDocument());
    expect(screen.queryByText('Shared')).not.toBeInTheDocument();
  });

  it('renders only company agents when scope="company"', async () => {
    const { base44 } = await import('@/api/base44Client');
    base44.entities.AgentApp.list.mockResolvedValue(sample);
    render(<MemoryRouter><AgentsView scope="company" /></MemoryRouter>);
    await waitFor(() => expect(screen.getAllByText('Shared').length).toBeGreaterThan(0));
    expect(screen.queryByText('Mine')).not.toBeInTheDocument();
  });

  it('renders both sections when scope is null (default)', async () => {
    const { base44 } = await import('@/api/base44Client');
    base44.entities.AgentApp.list.mockResolvedValue(sample);
    render(<MemoryRouter><AgentsView /></MemoryRouter>);
    await waitFor(() => {
      expect(screen.getByText('Mine')).toBeInTheDocument();
      expect(screen.getAllByText('Shared').length).toBeGreaterThan(0);
    });
  });
});
