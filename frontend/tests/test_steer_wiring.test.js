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

// Resolve the repo root from this file's own location (the old
// hardcoded '/root/zhanlu' broke when the repo moved to macOS).
// This file lives at <repo>/frontend/tests/, so the root is two levels up.
const REPO = path.resolve(__dirname, '..', '..');
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
    // The effectiveAgent branch must short-circuit to handleSteer when
    // BOTH the persistent-stream is active for this session AND
    // streamingConvId is set. The stream-state API evolved from
    // `streamState.isActive` to `stream.isActiveForSession(activeId)`
    // when streaming moved into PersistentStreamContext (survives page
    // navigation); the wiring contract is unchanged.
    expect(CHAT).toMatch(/effectiveAgent && stream\.isActiveForSession\(activeId\) && streamingConvId/);
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

describe('ChatInput.jsx streaming button contract', () => {
  it('renders ONLY the Stop button while streaming (no Steer, no mic)', () => {
    // Design change (2026-08-xx): the mid-turn Steer button and the
    // VoiceInput mic are deliberately HIDDEN while the agent responds —
    // "three icons in the corner was visually loud and the user asked
    // for just Stop during the response" (ChatInput.jsx comment). The
    // isStreaming branch must therefore contain exactly ONE button.
    const branch = CHAT_INPUT.match(/isStreaming \?\s*\(([\s\S]*?)\)\s*:\s*\(/);
    expect(branch).not.toBeNull();
    const branchBody = branch[1];
    const buttonOpens = (branchBody.match(/<button\b/g) || []).length;
    expect(buttonOpens).toBe(1);
    // The single streaming button is the Stop control (routes through
    // onStopAutomation when present for cooperative automation cancel).
    expect(branchBody).toMatch(/onStopAutomation/);
    expect(branchBody).toMatch(/onStop\b/);
  });

  it('mid-turn steering still exists via Chat.jsx handleSend routing', () => {
    // The steer affordance moved out of the streaming toolbar into the
    // main send path: while a stream is active, sending text routes to
    // handleSteer (asserted in the Chat.jsx describe above). ChatInput
    // itself must NOT render a second streaming button.
    expect(CHAT_INPUT).not.toMatch(/lang === 'en' \? 'Steer' : '引导'/);
  });
});
