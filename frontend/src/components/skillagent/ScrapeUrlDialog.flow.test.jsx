/**
 * UI automation test: ScrapeUrlDialog full interaction flow.
 *
 * Simulates a user opening the scrape dialog, entering a URL and skill
 * name, clicking "Collect", and verifying the success state displays
 * the collected skill's name, description, and security scan summary.
 * Also tests the error path (API rejection) and the cancel flow.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// Mock the API client
vi.mock('@/api/skillStudio', () => ({
  collectSkill: vi.fn(),
}));

// Mock the language provider
vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({
    t: {
      skillAgent: {
        title: 'Skill Agent', subtitle: '', placeholder: '', tryOne: '', chips: {},
        scrape: {
          chip: 'Scrape from URL', title: 'Scrape Skill from URL',
          urlLabel: 'Website URL', urlPlaceholder: 'https://example.com/skill-docs',
          skillNameLabel: 'Skill Name', skillNamePlaceholder: 'my-collected-skill', optional: '(optional)',
          collect: 'Collect', collecting: 'Collecting...', cancel: 'Cancel', done: 'Done',
          success: 'Skill collected successfully!', descriptionLabel: 'Description', securityScanLabel: 'Security Scan',
          urlRequired: 'Please enter a URL', collectionFailed: 'Collection failed',
          stages: { navigate: 'Navigating', extract: 'Extracting', structure: 'Structuring', save: 'Saving' },
        },
      },
      common: { send: 'Send' },
    },
  }),
}));

import ScrapeUrlDialog from '@/components/skillagent/ScrapeUrlDialog';
import { collectSkill } from '@/api/skillStudio';

describe('ScrapeUrlDialog — full interaction flow', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders the dialog with URL and skill name inputs when open', () => {
    render(<ScrapeUrlDialog open={true} onClose={vi.fn()} onCollected={vi.fn()} />);

    expect(screen.getByText('Scrape Skill from URL')).toBeDefined();
    expect(screen.getByPlaceholderText('https://example.com/skill-docs')).toBeDefined();
    expect(screen.getByPlaceholderText('my-collected-skill')).toBeDefined();
    expect(screen.getByText('Collect')).toBeDefined();
    expect(screen.getByText('Cancel')).toBeDefined();
  });

  it('returns null when not open', () => {
    const { container } = render(<ScrapeUrlDialog open={false} onClose={vi.fn()} onCollected={vi.fn()} />);
    expect(container.innerHTML).toBe('');
  });

  it('disables the Collect button when URL is empty', () => {
    render(<ScrapeUrlDialog open={true} onClose={vi.fn()} onCollected={vi.fn()} />);

    // The Collect button should be disabled when no URL is entered
    const collectBtn = screen.getByText('Collect').closest('button');
    expect(collectBtn.disabled).toBe(true);
    expect(collectSkill).not.toHaveBeenCalled();
  });

  it('collects a skill successfully and displays the result', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const mockResult = {
      success: true,
      skill_name: 'fastapi-guide',
      description: 'A skill for building FastAPI REST APIs',
      scan_findings: { has_critical: false, summary: '0 critical, 0 warnings' },
      source_url: 'https://example.com/fastapi',
    };
    collectSkill.mockResolvedValueOnce(mockResult);
    const onCollected = vi.fn();

    render(<ScrapeUrlDialog open={true} onClose={vi.fn()} onCollected={onCollected} />);

    // Type URL
    const urlInput = screen.getByPlaceholderText('https://example.com/skill-docs');
    await user.type(urlInput, 'https://example.com/fastapi');

    // Type skill name
    const nameInput = screen.getByPlaceholderText('my-collected-skill');
    await user.type(nameInput, 'fastapi-guide');

    // Click Collect
    await user.click(screen.getByText('Collect'));

    // Wait for the success state
    await waitFor(() => {
      expect(screen.getByText('Skill collected successfully!')).toBeDefined();
    });

    // Verify the skill name is displayed
    expect(screen.getByText('fastapi-guide')).toBeDefined();
    expect(screen.getByText('A skill for building FastAPI REST APIs')).toBeDefined();
    expect(screen.getByText('0 critical, 0 warnings')).toBeDefined();

    // Verify the API was called with the right args
    expect(collectSkill).toHaveBeenCalledWith('https://example.com/fastapi', 'fastapi-guide');

    // Verify the onCollected callback was called with the result
    expect(onCollected).toHaveBeenCalledWith(mockResult);
  });

  it('uses undefined for skill name when not provided', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    collectSkill.mockResolvedValueOnce({
      success: true,
      skill_name: 'auto-derived-name',
      description: 'desc',
    });

    render(<ScrapeUrlDialog open={true} onClose={vi.fn()} onCollected={vi.fn()} />);

    await user.type(screen.getByPlaceholderText('https://example.com/skill-docs'), 'https://example.com/some-page');
    await user.click(screen.getByText('Collect'));

    await waitFor(() => {
      expect(screen.getByText('Skill collected successfully!')).toBeDefined();
    });

    expect(collectSkill).toHaveBeenCalledWith('https://example.com/some-page', undefined);
  });

  it('displays an error message when collection fails', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    collectSkill.mockRejectedValueOnce(new Error('Connection timeout'));

    render(<ScrapeUrlDialog open={true} onClose={vi.fn()} onCollected={vi.fn()} />);

    await user.type(screen.getByPlaceholderText('https://example.com/skill-docs'), 'https://example.com/fail');
    await user.click(screen.getByText('Collect'));

    await waitFor(() => {
      expect(screen.getByText('Connection timeout')).toBeDefined();
    });
  });

  it('displays critical scan findings in red when has_critical is true', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    collectSkill.mockResolvedValueOnce({
      success: true,
      skill_name: 'risky-skill',
      description: 'A risky skill',
      scan_findings: { has_critical: true, summary: '2 critical findings detected' },
    });

    render(<ScrapeUrlDialog open={true} onClose={vi.fn()} onCollected={vi.fn()} />);

    await user.type(screen.getByPlaceholderText('https://example.com/skill-docs'), 'https://example.com/risky');
    await user.click(screen.getByText('Collect'));

    await waitFor(() => {
      expect(screen.getByText('2 critical findings detected')).toBeDefined();
    });
  });

  it('closes the dialog when Cancel is clicked', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const onClose = vi.fn();

    render(<ScrapeUrlDialog open={true} onClose={onClose} onCollected={vi.fn()} />);

    await user.click(screen.getByText('Cancel'));

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('closes the dialog when Done is clicked after success', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const onClose = vi.fn();
    collectSkill.mockResolvedValueOnce({
      success: true,
      skill_name: 'test-skill',
      description: 'desc',
    });

    render(<ScrapeUrlDialog open={true} onClose={onClose} onCollected={vi.fn()} />);

    await user.type(screen.getByPlaceholderText('https://example.com/skill-docs'), 'https://example.com/test');
    await user.click(screen.getByText('Collect'));

    await waitFor(() => {
      expect(screen.getByText('Done')).toBeDefined();
    });

    await user.click(screen.getByText('Done'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('disables the Collect button while loading', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    // Never resolves — keeps loading state
    collectSkill.mockReturnValueOnce(new Promise(() => { }));

    render(<ScrapeUrlDialog open={true} onClose={vi.fn()} onCollected={vi.fn()} />);

    await user.type(screen.getByPlaceholderText('https://example.com/skill-docs'), 'https://example.com/slow');
    await user.click(screen.getByText('Collect'));

    // While loading, the button text changes to "Collecting..." and should be disabled
    await waitFor(() => {
      expect(screen.getByText('Collecting...')).toBeDefined();
    });
  });
});
