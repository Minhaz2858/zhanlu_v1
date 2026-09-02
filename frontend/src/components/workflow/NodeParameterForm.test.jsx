/**
 * NodeParameterForm.jsx — source-contract tests for the non-IT-first schema
 * support. Same convention as WorkflowBuilderChat.test.jsx: pin the contract
 * without booting React (RAM-tight stack).
 *
 * Contracts pinned here:
 *   1. visible_when — conditional fields hide unless config matches
 *   2. Advanced (optional) collapsible section for power-user props
 *   3. Single-option auto-select in DB-backed pickers (connect once, no
 *      dropdown for users with exactly one database)
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './NodeParameterForm.jsx'), 'utf8');

describe('NodeParameterForm.jsx non-IT-first schema support', () => {
  it('hides conditional fields via visible_when evaluated against config', () => {
    expect(SOURCE).toMatch(/visible_when/);
    // Evaluates every {field: value} entry against the node config value.
    expect(SOURCE).toMatch(/config\?\.\[k\]\s*===\s*v/);
    // Hidden fields render nothing.
    expect(SOURCE).toMatch(/if\s*\(!visible\)\s*return\s*null/);
  });

  it('renders advanced props in a collapsible Advanced (optional) section', () => {
    expect(SOURCE).toMatch(/Advanced\s*\(optional\)|高级设置/);
    expect(SOURCE).toMatch(/<details/);
    // Basic and advanced are split by the p.advanced flag.
    expect(SOURCE).toMatch(/\.filter\(\(p\)\s*=>\s*!p\.advanced\)/);
    expect(SOURCE).toMatch(/\.filter\(\(p\)\s*=>\s*p\.advanced\)/);
  });

  it('auto-selects the single option in a picker (connect once, no dropdown)', () => {
    expect(SOURCE).toMatch(/options\.length\s*===\s*1/);
    expect(SOURCE).toMatch(/onChange\(options\[0\]\.value\)/);
    // Guarded by a ref so it fires once and cannot loop on onChange identity.
    expect(SOURCE).toMatch(/autoSelected/);
  });
});
