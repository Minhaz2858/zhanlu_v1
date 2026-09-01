import { describe, it, expect } from 'vitest';
import { partitionArtifacts } from './partitionArtifacts';

const automationArtifact = {
  id: 'file-1',
  type: 'html',
  file_name: 'Daily Sales Data Sync.html',
  title: 'Daily Sales Data Sync.html',
  file_size: 2048,
  file_url: '/api/automations/files/file-1/download',
  has_preview: true,
  source: 'automation_file',
};

const chatArtifact = {
  artifact_id: 'art-9',
  type: 'docx',
  title: 'Summary.docx',
  file_size: 1024,
};

const previewableChatArtifact = {
  artifact_id: 'art-10',
  type: 'html',
  title: 'report.html',
  has_preview: true,
};

describe('partitionArtifacts', () => {
  it('routes automation_file artifacts to inline, not cards', () => {
    const { inline, cards } = partitionArtifacts([automationArtifact]);
    expect(inline).toHaveLength(1);
    expect(cards).toHaveLength(0);
  });

  it('routes has_preview artifacts to inline, not cards', () => {
    const { inline, cards } = partitionArtifacts([previewableChatArtifact]);
    expect(inline).toHaveLength(1);
    expect(cards).toHaveLength(0);
  });

  it('keeps ordinary chat artifacts in cards only', () => {
    const { inline, cards } = partitionArtifacts([chatArtifact]);
    expect(inline).toHaveLength(0);
    expect(cards).toHaveLength(1);
  });

  it('never puts the same artifact in both lists (the duplicate-card bug)', () => {
    const { inline, cards } = partitionArtifacts([
      automationArtifact,
      chatArtifact,
      previewableChatArtifact,
    ]);
    const inlineIds = new Set(inline.map((a) => a.artifact_id || a.id));
    const cardIds = new Set(cards.map((a) => a.artifact_id || a.id));
    for (const id of inlineIds) {
      expect(cardIds.has(id)).toBe(false);
    }
    expect(inline).toHaveLength(2);
    expect(cards).toHaveLength(1);
  });

  it('handles null/empty input', () => {
    expect(partitionArtifacts(null)).toEqual({ inline: [], cards: [] });
    expect(partitionArtifacts(undefined)).toEqual({ inline: [], cards: [] });
    expect(partitionArtifacts([])).toEqual({ inline: [], cards: [] });
  });
});
