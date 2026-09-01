import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const previewWidgetSql = vi.fn();
vi.mock('@/api/dashboards', () => ({ previewWidgetSql: (...a) => previewWidgetSql(...a) }));

import WidgetEditorDrawer from './WidgetEditorDrawer';

beforeEach(() => { previewWidgetSql.mockReset(); });

const WIDGET = { id: 'w1', type: 'kpi', title: 'A', sql: 'SELECT 1 AS n', options: { unit: '%' } };

describe('WidgetEditorDrawer', () => {
  it('renders fields prefilled from widget', () => {
    render(<WidgetEditorDrawer widget={WIDGET} dashboardId="d1" params={{}} onApply={() => {}} onCancel={() => {}} />);
    expect(screen.getByDisplayValue('A')).toBeInTheDocument();
    expect(screen.getByDisplayValue('SELECT 1 AS n')).toBeInTheDocument();
    expect(screen.getByDisplayValue(/%/)).toBeInTheDocument(); // options JSON contains %
  });

  it('Run preview calls previewWidgetSql and renders columns + rows', async () => {
    previewWidgetSql.mockResolvedValue({ columns: ['n'], rows: [{ n: 1 }], error: null, truncated: false });
    render(<WidgetEditorDrawer widget={WIDGET} dashboardId="d1" params={{ from: '2026-07-01' }} onApply={() => {}} onCancel={() => {}} />);
    fireEvent.click(screen.getByText('Run preview'));
    await waitFor(() => expect(previewWidgetSql).toHaveBeenCalledWith('d1', { sql: 'SELECT 1 AS n', params: { from: '2026-07-01' } }));
    await waitFor(() => expect(screen.getByText('n')).toBeInTheDocument()); // column header
    expect(screen.getByText('1')).toBeInTheDocument(); // row value
  });

  it('preview error renders error line', async () => {
    previewWidgetSql.mockResolvedValue({ columns: [], rows: [], error: 'boom', truncated: false });
    render(<WidgetEditorDrawer widget={WIDGET} dashboardId="d1" params={{}} onApply={() => {}} onCancel={() => {}} />);
    fireEvent.click(screen.getByText('Run preview'));
    await waitFor(() => expect(screen.getByText(/boom/)).toBeInTheDocument());
  });

  it('Apply disabled when sql empty', () => {
    render(<WidgetEditorDrawer widget={null} dashboardId="d1" params={{}} onApply={() => {}} onCancel={() => {}} />);
    expect(screen.getByText('Apply')).toBeDisabled();
  });

  it('Apply calls onApply with widget (new id generated when blank)', () => {
    const onApply = vi.fn();
    render(<WidgetEditorDrawer widget={null} dashboardId="d1" params={{}} onApply={onApply} onCancel={() => {}} />);
    fireEvent.change(screen.getByPlaceholderText(/title/i), { target: { value: 'New' } });
    fireEvent.change(screen.getByPlaceholderText(/sql/i), { target: { value: 'SELECT 1 AS n' } });
    fireEvent.click(screen.getByText('Apply'));
    expect(onApply).toHaveBeenCalledTimes(1);
    const w = onApply.mock.calls[0][0];
    expect(w.title).toBe('New');
    expect(w.sql).toBe('SELECT 1 AS n');
    expect(w.id).toMatch(/^w_/);
  });

  it('Cancel calls onCancel', () => {
    const onCancel = vi.fn();
    render(<WidgetEditorDrawer widget={WIDGET} dashboardId="d1" params={{}} onApply={() => {}} onCancel={onCancel} />);
    fireEvent.click(screen.getByText('Cancel'));
    expect(onCancel).toHaveBeenCalled();
  });
});
