import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import ConfirmDialog from '@/components/ui/ConfirmDialog';

describe('ConfirmDialog', () => {
  it('renders nothing when closed', () => {
    const { container } = render(<ConfirmDialog open={false} title="X" onConfirm={() => {}} onCancel={() => {}} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders title + message + buttons when open', () => {
    render(
      <ConfirmDialog
        open
        title="Delete 'Awesome Skills'?"
        message="All 12 skills from this source will be removed."
        confirmLabel="Delete"
        destructive
        onConfirm={() => {}}
        onCancel={() => {}}
      />
    );
    expect(screen.getByText("Delete 'Awesome Skills'?")).toBeDefined();
    expect(screen.getByText('All 12 skills from this source will be removed.')).toBeDefined();
    expect(screen.getByText('Delete')).toBeDefined();
    expect(screen.getByText('Cancel')).toBeDefined();
  });

  it('fires onConfirm when confirm button clicked', () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <ConfirmDialog
        open
        title="X"
        confirmLabel="OK"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    fireEvent.click(screen.getByText('OK'));
    expect(onConfirm).toHaveBeenCalled();
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('fires onCancel when cancel button clicked', () => {
    const onCancel = vi.fn();
    render(
      <ConfirmDialog
        open
        title="X"
        onConfirm={() => {}}
        onCancel={onCancel}
      />
    );
    fireEvent.click(screen.getByText('Cancel'));
    expect(onCancel).toHaveBeenCalled();
  });

  it('disables both buttons when busy', () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <ConfirmDialog
        open
        title="X"
        confirmLabel="Working..."
        busy
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    const buttons = screen.getAllByRole('button');
    buttons.forEach((b) => expect(b.disabled).toBe(true));
    // The confirm click should not fire while busy.
    fireEvent.click(buttons[buttons.length - 1]);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('destructive variant uses red confirm button', () => {
    render(
      <ConfirmDialog
        open
        title="X"
        confirmLabel="Delete"
        destructive
        onConfirm={() => {}}
        onCancel={() => {}}
      />
    );
    const confirmBtn = screen.getByText('Delete');
    expect(confirmBtn.className).toContain('bg-red-500');
  });
});
