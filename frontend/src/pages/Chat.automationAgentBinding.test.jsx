/**
 * Regression (2026-08-11): automation-origin chats were not bound to the
 * real ``automation_agent`` — the frontend nulled out ``effectiveAgent``
 * for CREATE requests and injected an ``AUTOMATION_AGENT_PROMPT`` overlay
 * onto whatever agent resolved (e.g. a project-scoped agent),
 * whose report/data tools hijacked the free-text data-source answer.
 *
 * The fix binds automation-origin chats to the real ``automation_agent``
 * (hidden, is_system=True) with a focused toolset and a strict prompt
 * that mandates [[CLARIFY]] option cards and treats a data-source answer
 * as a configuration binding — never a report request.
 *
 * Source-text tests, same pattern as Chat.projectContextLeak.test.jsx —
 * no DOM, no router, no mocks. The contract is in the JSX.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './Chat.jsx'), 'utf8');

describe('Chat.jsx automation_agent binding', () => {
  it('does NOT null out effectiveAgent for create-automation requests', () => {
    // The old code had:
    //   if (effectiveAgent && isCreateAutomationRequest(fullText)) {
    //     effectiveAgent = null;
    //   }
    // This sent automation CREATE requests through the legacy non-agent
    // path with the AUTOMATION_AGENT_PROMPT overlay — which is how the
    // data-source answer got hijacked into report generation.
    // The new code must NOT contain this nulling.
    expect(SOURCE).not.toMatch(/if\s*\(\s*effectiveAgent\s*&&\s*isCreateAutomationRequest\(fullText\)\s*\)\s*\{\s*effectiveAgent\s*=\s*null\s*;?\s*\}/);
  });

  it('binds automation-origin chats to automation_agent', () => {
    // The new code must contain an isAutomationOrigin check that
    // resolves effectiveAgent to the automation_agent AgentApp row.
    expect(SOURCE).toMatch(/isAutomationOrigin/);
    expect(SOURCE).toMatch(/automation_agent/);
    // The binding must happen when isCreateAutomationRequest(fullText)
    // matches OR activeAgent is already automation_agent.
    const bindingBlock = SOURCE.match(/const isAutomationOrigin\s*=\s*([\s\S]{0,300}?);/);
    expect(bindingBlock, 'isAutomationOrigin declaration not found').not.toBeNull();
    const condition = bindingBlock[1];
    expect(condition).toMatch(/isCreateAutomationRequest\(fullText\)/);
    expect(condition).toMatch(/automation_agent/);
  });

  it('handleSelectOption sends a hidden system hint for automation-bound chats', () => {
    // When the conversation is automation-bound (latest assistant message
    // contains [[CLARIFY]], or activeAgent is automation_agent), the
    // option selection must be sent as a hidden 【系统提示】 that instructs
    // the agent to bind the data source — NOT as a visible user message.
    const selectBlock = SOURCE.match(/function handleSelectOption[\s\S]{0,1500}?\n  \}/);
    expect(selectBlock, 'handleSelectOption function not found').not.toBeNull();
    const body = selectBlock[0];
    // Must check for [[CLARIFY]] in the latest assistant message.
    expect(body).toMatch(/\[\[CLARIFY\]\]/);
    // Must check for automation_agent in activeAgent.
    expect(body).toMatch(/activeAgent.*automation_agent/);
    // Must send a hidden system hint (【系统提示】) with data_source_id binding instruction.
    expect(body).toMatch(/【系统提示】/);
    expect(body).toMatch(/data_source_id/);
    expect(body).toMatch(/hidden:\s*true/);
  });

  it('dismisses pendingDraft when create_automation tool call succeeds', () => {
    // The done event handler must check toolCalls for a create_automation
    // tool call and dismiss pendingDraft + dispatch zhanlu:automation-created.
    const doneBlock = SOURCE.match(/create_automation[\s\S]{0,600}?setPendingDraft\(null\)/);
    expect(doneBlock, 'create_automation → setPendingDraft(null) dismissal not found').not.toBeNull();
    const block = doneBlock[0];
    expect(block).toMatch(/zhanlu:automation-created/);
    expect(block).toMatch(/toolCalls\.some/);
  });
});
