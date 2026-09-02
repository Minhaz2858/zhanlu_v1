/**
 * WorkflowCanvas.jsx — source-contract test for schema-default seeding on
 * node drop. Dropping a node used to create `config: {}` — the db_query
 * runtime then interpreted the missing `mode` as SQL, defeating the
 * NL-first default. New nodes must seed config from the registry defaults.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './WorkflowCanvas.jsx'), 'utf8');

describe('WorkflowCanvas.jsx node drop config seeding', () => {
  it('reads the node registry to seed config defaults on drop', () => {
    expect(SOURCE).toMatch(/useNodeRegistry/);
    expect(SOURCE).toMatch(/nodeTypes\?\.find/);
  });

  it('no longer creates bare config: {} on drop', () => {
    // The old line was `data: { label: type, config: {} }`.
    expect(SOURCE).not.toMatch(/config:\s*\{\s*\}/);
  });

  it('seeds every prop default into config (skipping empty-string defaults)', () => {
    expect(SOURCE).toMatch(/p\.default/);
    expect(SOURCE).toMatch(/config\[p\.name\]\s*=\s*p\.default/);
  });
});
