import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import DashboardCard from './DashboardCard';

vi.mock('@/api/dashboards', () => ({ getDashboard: vi.fn(), queryDashboard: vi.fn() }));

it('renders metadata and fires onOpen', () => {
  const onOpen = vi.fn();
  render(
    <DashboardCard
      artifact={{ title: 'Sales', datasource_name: 'sales_db', widget_count: 4 }}
      onOpen={onOpen}
    />,
  );
  expect(screen.getByText('Sales')).toBeInTheDocument();
  expect(screen.getByText(/sales_db/)).toBeInTheDocument();
  expect(screen.getByText(/4/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: /open/i }));
  expect(onOpen).toHaveBeenCalled();
});
