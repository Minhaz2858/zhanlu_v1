import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './ProjectDetail.jsx'), 'utf8');

describe('ProjectDetail Catalog section IA', () => {
  it('includes data_map in sidebar sections and keeps resources as sheet action', () => {
    expect(SOURCE).toMatch(/\{ key: 'data_map', icon: Database, scrollable: true \}/);
    expect(SOURCE).toMatch(/\{ key: 'resources', icon: Layers, scrollable: false \}/);
  });

  it('renders a dedicated Data Map section in main content', () => {
    expect(SOURCE).toMatch(/id="project-section-data_map"/);
    expect(SOURCE).toMatch(/<DataMapSection/);
    expect(SOURCE).toMatch(/\{isEn \? 'Catalog' : '目录'\}/);
  });

  it('keeps resources sheet wiring available', () => {
    expect(SOURCE).toMatch(/<Sheet open=\{resourcesOpen\} onOpenChange=\{setResourcesOpen\}>/);
    expect(SOURCE).toMatch(/setResourcesOpen\(true\)/);
  });
});
