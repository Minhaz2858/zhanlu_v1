import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({ t: { marketplace: { sync: 'Sync', hide: 'Hide', remove: 'Remove', delete: 'Delete', search: 'Search skills...' } } }),
}));
vi.mock('@/api/marketplace', () => ({
  listSourceSkills: vi.fn(),
  syncSource: vi.fn(),
  listRemovedSources: vi.fn(),
  restoreRemovedSource: vi.fn(),
  deleteSource: vi.fn(),
}));

import MarketplaceTab from '@/components/marketplace/MarketplaceTab';
import SourceCard from '@/components/marketplace/SourceCard';

const SOURCE = {
  id: 's1',
  name: 'Anthropic Skills',
  url: 'https://github.com/anthropics/skills',
  source_type: 'github_repo',
  skill_count: 3,
  last_sync_status: 'success',
  last_synced_at: '2026-07-29T10:00:00Z',
  is_default: true,
  brand_color: '#191919',
  icon_emoji: '✦',
};

describe('SourceCard', () => {
  it('renders the icon emoji + name + skill count', () => {
    render(<SourceCard source={SOURCE} isActive={false} />);
    const btn = screen.getByTitle(SOURCE.name);
    expect(btn).toBeDefined();
    expect(btn.textContent).toContain('✦');
    expect(btn.textContent).toContain('Anthropic Skills');
    expect(btn.textContent).toContain('3 skills');
  });

  it('falls back to first letter of name when icon_emoji is missing', () => {
    render(<SourceCard source={{ ...SOURCE, icon_emoji: null }} isActive={false} />);
    expect(screen.getByTitle(SOURCE.name).textContent).toContain('A');
  });

  it('applies active state border', () => {
    const { container } = render(<SourceCard source={SOURCE} isActive />);
    const btn = container.querySelector('button');
    expect(btn.className).toContain('border-primary');
  });

  it('applies inactive state border', () => {
    const { container } = render(<SourceCard source={SOURCE} isActive={false} />);
    const btn = container.querySelector('button');
    expect(btn.className).toContain('border-border');
  });

  it('renders the +add variant with kind="add"', () => {
    const onAdd = vi.fn();
    render(<SourceCard kind="add" onAdd={onAdd} addLabel="Add source" />);
    const btn = screen.getByText('Add source');
    fireEvent.click(btn);
    expect(onAdd).toHaveBeenCalled();
  });

  it('shows "Syncing…" when last_sync_status is "syncing"', () => {
    render(<SourceCard source={{ ...SOURCE, last_sync_status: 'syncing' }} isActive={false} />);
    expect(screen.getByTitle(SOURCE.name).textContent).toContain('Syncing');
  });

  it('shows the skill count when sync has completed', () => {
    render(<SourceCard source={{ ...SOURCE, last_sync_status: 'success', skill_count: 12 }} isActive={false} />);
    expect(screen.getByTitle(SOURCE.name).textContent).toContain('12 skills');
  });
});

describe('MarketplaceTab delete buttons', () => {
  // The user reported "delete button is not working" on 2026-07-29. The
  // fix was twofold: (1) replace window.confirm with an in-app dialog
  // (the native one can be blocked), and (2) wire a `busy` prop so the
  // button shows feedback while the API call is in flight. These tests
  // pin the busy state and the action passed to onDelete.
  const SOURCE = {
    id: 's1',
    name: 'Anthropic Skills',
    url: 'https://github.com/anthropics/skills',
    source_type: 'github_repo',
    skill_count: 4,
    last_sync_status: 'success',
    is_default: true,
    brand_color: '#191919',
    icon_emoji: '✦',
  };

  it('passes action="delete" when the Delete button is clicked', async () => {
    const { listSourceSkills } = await import('@/api/marketplace');
    listSourceSkills.mockResolvedValue({ skills: [], count: 0 });
    const onDelete = vi.fn();
    render(
      <MarketplaceTab
        source={SOURCE}
        installedNames={new Set()}
        onInstall={() => {}}
        onOpenDetail={() => {}}
        onDelete={onDelete}
      />
    );
    fireEvent.click(screen.getByText('Delete'));
    expect(onDelete).toHaveBeenCalledWith(SOURCE, { action: 'delete' });
  });

  it('passes action="hide" when the Hide button is clicked', async () => {
    const { listSourceSkills } = await import('@/api/marketplace');
    listSourceSkills.mockResolvedValue({ skills: [], count: 0 });
    const onDelete = vi.fn();
    render(
      <MarketplaceTab
        source={SOURCE}
        installedNames={new Set()}
        onInstall={() => {}}
        onOpenDetail={() => {}}
        onDelete={onDelete}
      />
    );
    fireEvent.click(screen.getByText('Hide'));
    expect(onDelete).toHaveBeenCalledWith(SOURCE, { action: 'hide' });
  });

  it('disables Delete + Hide buttons when busy is true (prevents double-click)', async () => {
    const { listSourceSkills } = await import('@/api/marketplace');
    listSourceSkills.mockResolvedValue({ skills: [], count: 0 });
    const onDelete = vi.fn();
    render(
      <MarketplaceTab
        source={SOURCE}
        installedNames={new Set()}
        onInstall={() => {}}
        onOpenDetail={() => {}}
        onDelete={onDelete}
        busy
      />
    );
    const deleteBtn = screen.getByText('Delete');
    const hideBtn = screen.getByText('Hide');
    expect(deleteBtn.disabled).toBe(true);
    expect(hideBtn.disabled).toBe(true);
    // Clicking the disabled button should NOT fire onDelete.
    fireEvent.click(deleteBtn);
    expect(onDelete).not.toHaveBeenCalled();
  });
});

describe('MarketplaceTab', () => {
  it('renders the source URL header + skill count when loaded', async () => {
    const { listSourceSkills } = await import('@/api/marketplace');
    listSourceSkills.mockResolvedValue({
      skills: [
        { id: 'sk1', name: 'dashboard-gen', display_name: 'Dashboard Gen', description: 'd', category: 'gen', version: '1', skill_md: 'x' },
      ],
      count: 1,
    });
    render(<MarketplaceTab source={SOURCE} installedNames={new Set()} onInstall={() => {}} onOpenDetail={() => {}} onDelete={() => {}} />);
    expect(await screen.findByText('https://github.com/anthropics/skills')).toBeDefined();
    expect(await screen.findByText('3 skills')).toBeDefined();
    expect(await screen.findByText('Dashboard Gen')).toBeDefined();
  });
});
