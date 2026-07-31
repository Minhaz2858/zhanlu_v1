/**
 * Tests for the mid-turn steer frontend wiring (P2 Task 3).
 *
 * Strategy: structural/textual assertions on the modified frontend
 * modules. The frontend is React+Vite; we don't spin up a browser
 * here. These tests guard against accidental removal of the steer
 * integration in future edits.
 */

import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';

const REPO = '/root/zhanlu';
const API_HELPERS = fs.readFileSync(path.join(REPO, 'frontend/src/api/agentEnhanced.js'), 'utf8');
const CHAT = fs.readFileSync(path.join(REPO, 'frontend/src/pages/Chat.jsx'), 'utf8');
const CHAT_INPUT = fs.readFileSync(path.join(REPO, 'frontend/src/components/chat/ChatInput.jsx'), 'utf8');

describe('steerAgentConversation helper (agentEnhanced.js)', () => {
  it('exports steerAgentConversation', () => {
    expect(API_HELPERS).toMatch(/export async function steerAgentConversation/);
  });

  it('posts to the steer endpoint with the right shape', () => {
    expect(API_HELPERS).toMatch(/agents\/conversations\/\$\{conversationId\}\/steer/);
    expect(API_HELPERS).toMatch(/method:\s*'POST'/);
    expect(API_HELPERS).toMatch(/JSON\.stringify\(\{\s*message\s*\}\)/);
  });

  it('handles 429 (queue full) with a user-friendly error', () => {
    expect(API_HELPERS).toMatch(/resp\.status === 429/);
    expect(API_HELPERS).toMatch(/Steer queue is full/);
  });

  it('handles 404 (conversation not found)', () => {
    expect(API_HELPERS).toMatch(/resp\.status === 404/);
    expect(API_HELPERS).toMatch(/Conversation not found/);
  });
});

describe('Chat.jsx steer integration', () => {
  it('imports steerAgentConversation', () => {
    expect(CHAT).toMatch(/import\s*\{[^}]*steerAgentConversation[^}]*\}\s*from\s*'@\/api\/agentEnhanced'/);
  });

  it('declares streamingConvId and steerMarkers state', () => {
    expect(CHAT).toMatch(/useState\(null\);\s*\/\/ P2: conv id of the in-flight v3 stream/);
    expect(CHAT).toMatch(/useState\(\[\]\);\s*\/\/ P2: inline markers/);
  });

  it('routes handleSend to handleSteer when streaming is active', () => {
    // The effectiveAgent (or activeAgent) branch must short-circuit to
    // handleSteer when both streamState.isActive AND streamingConvId
    // are set. We allow either name because the variable was renamed
    // from activeAgent -> effectiveAgent in a prior refactor; the test
    // is checking the wiring, not the specific identifier.
    expect(CHAT).toMatch(/(?:activeAgent|effectiveAgent)\s*&&\s*streamState\.isActive\s*&&\s*streamingConvId/);
    expect(CHAT).toMatch(/return handleSteer\(fullText\)/);
  });

  it('defines handleSteer that posts via steerAgentConversation', () => {
    expect(CHAT).toMatch(/async function handleSteer\(text\)/);
    expect(CHAT).toMatch(/await steerAgentConversation\(streamingConvId, trimmed\)/);
  });

  it('sets streamingConvId at the start of handleAgentSend', () => {
    expect(CHAT).toMatch(/setStreamingConvId\(convId\)/);
  });

  it('clears streamingConvId at the end of handleAgentSend', () => {
    expect(CHAT).toMatch(/setStreamingConvId\(\(cur\)\s*=>\s*\(cur === convId \? null : cur\)\)/);
  });

  it('handles type === "steer" SSE events', () => {
    expect(CHAT).toMatch(/evt\.type === 'steer' && Array\.isArray\(evt\.messages\)/);
  });

  it('renders steer markers in the JSX', () => {
    expect(CHAT).toMatch(/steerMarkers\.length > 0/);
    expect(CHAT).toMatch(/→ steer/);
    expect(CHAT).toMatch(/→ 引导/);
  });

  it('clears steerMarkers when a new stream starts', () => {
    expect(CHAT).toMatch(/setSteerMarkers\(\[\]\);\s*\/\/ clear any markers from a prior stream/);
  });
});

describe('ChatInput.jsx steer button', () => {
  it('renders BOTH Stop and a Steer button while streaming', () => {
    // The isStreaming branch must now contain two buttons, not one.
    const branch = CHAT_INPUT.match(/isStreaming \?\s*\(([\s\S]*?)\)\s*:\s*\(/);
    expect(branch).not.toBeNull();
    const branchBody = branch[1];
    // Count <button tags in the branch.
    const buttonOpens = (branchBody.match(/<button\b/g) || []).length;
    expect(buttonOpens).toBe(2);
  });

  it('Steer button label is language-aware', () => {
    expect(CHAT_INPUT).toMatch(/lang === 'en' \? 'Steer' : '引导'/);
  });

  it('Steer button is disabled when input is empty', () => {
    expect(CHAT_INPUT).toMatch(/disabled=\{!value\.trim\(\)\}/);
  });
});
