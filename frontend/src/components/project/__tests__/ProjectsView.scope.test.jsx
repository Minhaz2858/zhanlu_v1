import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ProjectsView from '@/components/project/ProjectsView';

vi.mock('@/api/base44Client', () => ({
  base44: { entities: { Project: { list: vi.fn() }, AgentApp: { list: vi.fn(), filter: vi.fn() }, UserFile: { list: vi.fn(), filter: vi.fn() }, KnowledgeBase: { filter: vi.fn() } } },
}));

vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({ t: {}, lang: 'en' }),
}));

const sample = [
  { id: '1', name: 'Mine', resource_type: 'personal', status: 'active', updated_date: new Date().toISOString() },
  { id: '2', name: 'Shared', resource_type: 'company', is_shared_with_me: true, status: 'active', updated_date: new Date().toISOString() },
];

describe('ProjectsView scope', () => {
  it('renders only personal projects when scope="personal"', async () => {
    const { base44 } = await import('@/api/base44Client');
    base44.entities.Project.list.mockResolvedValue(sample);
    base44.entities.AgentApp.filter.mockResolvedValue([]);
    base44.entities.UserFile.filter.mockResolvedValue([]);
    base44.entities.KnowledgeBase.filter.mockResolvedValue([]);
    render(<MemoryRouter><ProjectsView scope="personal" /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText('Mine')).toBeInTheDocument());
    expect(screen.queryByText('Shared')).not.toBeInTheDocument();
  });

  it('renders only company projects when scope="company"', async () => {
    const { base44 } = await import('@/api/base44Client');
    base44.entities.Project.list.mockResolvedValue(sample);
    base44.entities.AgentApp.filter.mockResolvedValue([]);
    base44.entities.UserFile.filter.mockResolvedValue([]);
    base44.entities.KnowledgeBase.filter.mockResolvedValue([]);
    render(<MemoryRouter><ProjectsView scope="company" /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText('Shared')).toBeInTheDocument());
    expect(screen.queryByText('Mine')).not.toBeInTheDocument();
  });

  it('renders both sections when scope is null (default)', async () => {
    const { base44 } = await import('@/api/base44Client');
    base44.entities.Project.list.mockResolvedValue(sample);
    base44.entities.AgentApp.filter.mockResolvedValue([]);
    base44.entities.UserFile.filter.mockResolvedValue([]);
    base44.entities.KnowledgeBase.filter.mockResolvedValue([]);
    render(<MemoryRouter><ProjectsView /></MemoryRouter>);
    await waitFor(() => {
      expect(screen.getByText('Mine')).toBeInTheDocument();
      expect(screen.getByText('Shared')).toBeInTheDocument();
    });
  });
});
