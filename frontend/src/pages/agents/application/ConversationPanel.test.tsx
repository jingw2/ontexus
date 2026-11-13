import '@/i18n'
import { describe, expect, it, vi } from 'vitest'
import { waitFor } from '@testing-library/react'
import { render, screen } from '@testing-library/react'
import ConversationPanel from './ConversationPanel'
import type { AgentMessage } from '@/api/agentSessions'
import { initialStreamState, type StreamState } from '@/api/agentStream'
import { apiClient } from '@/api/client'

function renderPanel(messages: AgentMessage[], stream: StreamState = initialStreamState) {
  return render(<ConversationPanel messages={messages} stream={stream} clarification={null}
    pendingApprovalId={null} onSend={() => {}} onAnswerClarification={() => {}}
    onApprovalResolved={() => {}} onRetry={() => {}} />)
}

describe('P4A-CONVERSATION', () => {
  it('hides prior-turn evidence synchronously when the turn changes', async () => {
    const get = vi.spyOn(apiClient, 'get').mockImplementation(url => url.includes('/old/')
      ? Promise.resolve({ items: [{ sequence: 1, event_type: 'final_response', payload: { audit_event_id: 'old-audit' } }] }) as never
      : new Promise(() => {}))
    const props = { messages: [], stream: { ...initialStreamState, terminal: true }, clarification: null,
      pendingApprovalId: null, onSend: () => {}, onAnswerClarification: () => {}, onApprovalResolved: () => {}, onRetry: () => {} }
    try {
      const view = render(<ConversationPanel {...props} turnId="old" />)
      await screen.findByText('old-audit')
      view.rerender(<ConversationPanel {...props} turnId="new" />)
      expect(screen.queryByTestId('journey-evidence')).toBeNull()
    } finally { get.mockRestore() }
  })

  it('strips the legacy canned "Answer for …" prefix from persisted messages', () => {
    const messages: AgentMessage[] = [{
      id: 'm-1', session_id: 's-1', turn_id: 't-1', role: 'assistant',
      ordinal: 2, content: 'Answer for 库存低于安全线的订单有哪些？',
    }]
    renderPanel(messages)
    // the prefix is never rendered — only the actual answer text
    expect(screen.getByText('库存低于安全线的订单有哪些？')).toBeTruthy()
    expect(screen.queryByText(/Answer for/)).toBeNull()
  })

  it('renders a normal assistant answer verbatim', () => {
    const messages: AgentMessage[] = [{
      id: 'm-2', session_id: 's-1', turn_id: 't-1', role: 'assistant',
      ordinal: 2, content: '真实回答：库存充足',
    }]
    renderPanel(messages)
    expect(screen.getByText('真实回答：库存充足')).toBeTruthy()
  })

  it('renders the streamed final_response message without the prefix', () => {
    const stream: StreamState = {
      ...initialStreamState, phase: 'terminal', terminal: true,
      events: [{ event: 'final_response', data: { message: 'Answer for 问题' }, sequence: 2 }],
    }
    renderPanel([], stream)
    expect(screen.getByText('问题')).toBeTruthy()
  })

  it('renders Markdown in an assistant answer as real formatted elements', () => {
    const messages: AgentMessage[] = [{
      id: 'm-3', session_id: 's-1', turn_id: 't-1', role: 'assistant',
      ordinal: 2, content: '**加粗**内容，以及一个列表：\n\n- 项目一\n- 项目二',
    }]
    renderPanel(messages)
    const bold = screen.getByText('加粗')
    expect(bold.tagName).toBe('STRONG')
    expect(screen.getByText('项目一').closest('ul')).toBeTruthy()
  })

  it('never renders raw Markdown syntax characters for the assistant', () => {
    const messages: AgentMessage[] = [{
      id: 'm-4', session_id: 's-1', turn_id: 't-1', role: 'assistant',
      ordinal: 2, content: '**加粗**',
    }]
    renderPanel(messages)
    expect(screen.queryByText('**加粗**')).toBeNull()
  })

  it('gives the user bubble the blue fill, and keeps user text un-rendered as Markdown', () => {
    const messages: AgentMessage[] = [{
      id: 'm-5', session_id: 's-1', turn_id: 't-1', role: 'user',
      ordinal: 1, content: '**not bold**',
    }]
    renderPanel(messages)
    const bubble = screen.getByText('**not bold**')
    expect(bubble.className).toContain('bg-blue-600')
  })

  it('shows the model\'s answer live while thinking, polling the answer-stream endpoint', async () => {
    const get = vi.spyOn(apiClient, 'get').mockImplementation((url: string) => {
      if (url.includes('/answer-stream')) return Promise.resolve({ text: '正在生成的答案…' } as never)
      return Promise.resolve({ items: [] } as never)
    })
    try {
      render(<ConversationPanel messages={[]} stream={{ ...initialStreamState, phase: 'streaming' }} clarification={null}
        pendingApprovalId={null} onSend={() => {}} onAnswerClarification={() => {}}
        onApprovalResolved={() => {}} onRetry={() => {}} turnId="t-1" />)
      expect(await screen.findByTestId('thinking-live-text')).toHaveTextContent('正在生成的答案…')
      expect(get).toHaveBeenCalledWith(expect.stringContaining('/agent-turns/t-1/answer-stream'))
    } finally {
      get.mockRestore()
    }
  })

  it('shows only the spinner (no live-text bubble) before the first chunk arrives', () => {
    const messages: AgentMessage[] = []
    render(<ConversationPanel messages={messages} stream={{ ...initialStreamState, phase: 'connecting' }} clarification={null}
      pendingApprovalId={null} onSend={() => {}} onAnswerClarification={() => {}}
      onApprovalResolved={() => {}} onRetry={() => {}} turnId="t-1" />)
    expect(screen.getByTestId('thinking-panel')).toBeTruthy()
    expect(screen.queryByTestId('thinking-live-text')).toBeNull()
  })

  it('renders the persisted business-journey process in sequence without payload content', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({ items: [
      { id: 'e1', turn_id: 't-1', sequence: 1, event_type: 'turn_started', payload: {} },
      { id: 'e2', turn_id: 't-1', sequence: 2, event_type: 'resolve_snapshot', payload: {} },
      { id: 'e3', turn_id: 't-1', sequence: 3, event_type: 'model_call', payload: { call_kind: 'agent_initial' } },
      { id: 'e4', turn_id: 't-1', sequence: 4, event_type: 'tool_executed', payload: {} },
      { id: 'e5', turn_id: 't-1', sequence: 5, event_type: 'model_call', payload: { call_kind: 'agent_final' } },
      { id: 'e6', turn_id: 't-1', sequence: 6, event_type: 'final_response', payload: {} },
      { id: 'e7', turn_id: 't-1', sequence: 7, event_type: 'turn_succeeded', payload: {} },
    ] } as never)
    try {
      render(<ConversationPanel messages={[]} stream={{ ...initialStreamState, terminal: true, phase: 'terminal' }} clarification={null}
        pendingApprovalId={null} onSend={() => {}} onAnswerClarification={() => {}}
        onApprovalResolved={() => {}} onRetry={() => {}} turnId="t-1" />)
      await waitFor(() => expect(screen.getByTestId('journey-process-order')).toHaveTextContent(
        'turn_started → resolve_snapshot → model_call → tool_executed → model_call → final_response → turn_succeeded',
      ))
    } finally {
      get.mockRestore()
    }
  })
})
