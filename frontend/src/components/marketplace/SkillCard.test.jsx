import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import SkillCard from '@/components/marketplace/SkillCard';

vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({ t: { marketplace: { add: 'Add', added: 'Added' } } }),
}));

const SKILL = {
  id: 's1', name: 'data-viz', display_name: 'Data Viz',
  description: 'Create beautiful data visualizations',
  category: 'data', version: '1.0.0', author: 'tester',
  tags: ['charts', 'visualization'], source_url: 'https://example.com',
  install_count: 5,
};

describe('SkillCard', () => {
  it('renders skill name, description, category, and tags', () => {
    render(<SkillCard skill={SKILL} isInstalled={false} onInstall={vi.fn()} onOpenDetail={vi.fn()} />);
    expect(screen.getByText('Data Viz')).toBeDefined();
    expect(screen.getByText('Create beautiful data visualizations')).toBeDefined();
    expect(screen.getByText('data')).toBeDefined();
    expect(screen.getByText('#charts')).toBeDefined();
    expect(screen.getByText('#visualization')).toBeDefined();
  });

  it('shows Add button when not installed', () => {
    render(<SkillCard skill={SKILL} isInstalled={false} onInstall={vi.fn()} onOpenDetail={vi.fn()} />);
    expect(screen.getByText('Add')).toBeDefined();
  });

  it('shows Added badge when installed', () => {
    render(<SkillCard skill={SKILL} isInstalled={true} onInstall={vi.fn()} onOpenDetail={vi.fn()} />);
    expect(screen.getByText('Added')).toBeDefined();
  });

  it('calls onInstall when Add button is clicked', () => {
    const onInstall = vi.fn();
    render(<SkillCard skill={SKILL} isInstalled={false} onInstall={onInstall} onOpenDetail={vi.fn()} />);
    fireEvent.click(screen.getByText('Add'));
    expect(onInstall).toHaveBeenCalledWith(SKILL);
  });

  it('calls onOpenDetail when card title is clicked', () => {
    const onOpenDetail = vi.fn();
    render(<SkillCard skill={SKILL} isInstalled={false} onInstall={vi.fn()} onOpenDetail={onOpenDetail} />);
    fireEvent.click(screen.getByText('Data Viz'));
    expect(onOpenDetail).toHaveBeenCalledWith(SKILL);
  });
});
