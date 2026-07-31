import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';

// --- mock everything MySpace pulls in so the test stays focused on the
//     Dashboards tab wiring (list + same-page popup) ---
vi.mock('@/api/dashboards', () => ({
  listDashboards: vi.fn(),
  getDashboard: vi.fn(),
  queryDashboard: vi.fn(),
  deleteDashboard: vi.fn(),
}));
vi.mock('@/api/base44Client', () => ({
  base44: { entities: new Proxy({}, { get: () => ({ list: async () => [] }) }) },
}));
vi.mock('@/lib/AuthContext', () => ({ useAuth: () => ({ user: { id: 'u1', org_id: 'o1' } }) }));
vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({
    lang: 'en',
    t: { mySpace: { title: 'My Space', subtitle: '', tabs: { project: 'Projects', agent: 'Agents', file: 'Files', kb: 'KB', dashboard: 'Dashboards' }, empty: 'No', emptyHint: '' }, common: { new: 'New', run: 'Run', delete: 'Delete' }, createDialog: { resourceLabels: {} }, automation: { ungrouped: '—' } },
  }),
}));
vi.mock('@/lib/useTranslate', () => ({ useTranslate: () => (s) => s }));
vi.mock('@/lib/draftManager', () => ({ hasDraft: () => false, clearDraft: () => {}, flushDraft: () => {}, loadDraft: () => null }));
vi.mock('@/lib/systemAgents', () => ({ filterUserAgents: (a) => a }));
vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn(), useSearchParams: () => [new URLSearchParams(), vi.fn()] }));
vi.mock('@/components/PageHeader', () => ({ default: () => <div /> }));
vi.mock('@/components/CreateResourceDialog', () => ({ default: () => null }));
vi.mock('@/components/kb/KbSetupDialog', () => ({ default: () => null }));
vi.mock('@/components/kb/KbCard', () => ({ default: () => null }));
vi.mock('@/components/files/FilesView', () => ({ default: () => null }));
vi.mock('@/components/project/ProjectsView', () => ({ default: () => null }));
vi.mock('@/components/project/ProjectCreateDialog', () => ({ default: () => null }));
vi.mock('@/components/agentbuilder/StepsAgentBuilder', () => ({ default: () => null }));
vi.mock('@/components/ui/button', () => ({ Button: (p) => <button {...p} /> }));

import MySpace from './MySpace';
import * as dashApi from '@/api/dashboards';

beforeEach(() => {
  dashApi.listDashboards.mockReset();
  dashApi.getDashboard.mockReset();
  dashApi.queryDashboard.mockReset();
  dashApi.deleteDashboard.mockReset();
  dashApi.getDashboard.mockResolvedValue({ id: 'd1', name: 'Sales', refresh_interval_seconds: 30, definition: { widgets: [] } });
  dashApi.queryDashboard.mockResolvedValue({ results: {}, refreshed_at: '2026-07-29T00:00:00Z' });
});

it('lists dashboards in the Dashboards tab and opens popup on click', async () => {
  dashApi.listDashboards.mockResolvedValue([{ id: 'd1', name: 'Sales', description: 'rev' }]);
  render(<MySpace />);
  fireEvent.click(screen.getByText('Dashboards'));
  await waitFor(() => expect(screen.getByText('Sales')).toBeInTheDocument());
  fireEvent.click(screen.getByText('Sales'));
  await waitFor(() => expect(dashApi.getDashboard).toHaveBeenCalledWith('d1'));
});
