/**
 * Contract tests for the SessionList global chat-history search
 * (Kimi/GPT-style). Source-text style: pins the search input, the
 * debounced chatSearch call, the results-replace-groups behavior, and
 * the select-on-click wiring without needing a DOM render.
 */
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';
import { describe, it, expect } from 'vitest';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const src = readFileSync(resolve(__dirname, './SessionList.jsx'), 'utf8');

describe('SessionList global chat search', () => {
  it('imports chatSearch (and chatShare) from agentEnhanced', () => {
    expect(src).toMatch(/import\s*\{\s*chatSearch[\s\S]*?\}\s*from\s*'@\/api\/agentEnhanced'/);
  });

  it('imports the Search icon from lucide-react', () => {
    expect(src).toMatch(/import\s*\{[^}]*\bSearch\b[^}]*\}\s*from\s*'lucide-react'/);
  });

  it('renders the search input with a stable testid', () => {
    expect(src).toMatch(/data-testid="session-search-input"/);
    expect(src).toMatch(/placeholder=\{t\.sessionList\.searchPlaceholder\}/);
  });

  it('shows a clear button only while a query is active', () => {
    expect(src).toMatch(/data-testid="session-search-clear"/);
    expect(src).toMatch(/\{searchQuery && \(/);
  });

  it('debounces the search call at 250ms', () => {
    expect(src).toMatch(/setTimeout\(async \(\) => \{/);
    expect(src).toMatch(/250\);/);
    expect(src).toMatch(/return \(\) => clearTimeout\(timer\);/);
    expect(src).toMatch(/chatSearch\(q, 20\)/);
  });

  it('replaces the session groups with results while a query is active', () => {
    expect(src).toMatch(/data-testid="session-search-results"/);
    // groups + empty state are hidden during search
    expect(src).toMatch(/!searchQuery\.trim\(\) && sessions\.length === 0/);
    expect(src).toMatch(/!searchQuery\.trim\(\) && groups\.map/);
  });

  it('renders a result row per match with title/agent/snippet', () => {
    expect(src).toMatch(/data-testid="session-search-result"/);
    expect(src).toMatch(/r\.matches\?\.\[0\]\?\.snippet/);
    expect(src).toMatch(/\{r\.agent_name && \(/);
  });

  it('selects the session and clears the query on result click', () => {
    expect(src).toMatch(/function handleSearchSelect\(id\) \{/);
    expect(src).toMatch(/setSearchQuery\(''\);/);
    expect(src).toMatch(/handleSelectSession\(id\);/);
    expect(src).toMatch(/onClick=\{\(\) => handleSearchSelect\(r\.session_id\)\}/);
  });

  it('shows an empty state when a search returns nothing', () => {
    expect(src).toMatch(/t\.sessionList\.searchEmpty/);
    expect(src).toMatch(/searchResults\.length === 0/);
  });
});
