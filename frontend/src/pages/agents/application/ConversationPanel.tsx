import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Loader2 } from 'lucide-react'
import type { AgentMessage } from '@/api/agentSessions'
import type { StreamState } from '@/api/agentStream'
import { apiClient } from '@/api/client'
import { renderMarkdown } from '@/security/sanitize'
import ActionApprovalCard, { GovernedPlanPanel } from './ActionApprovalCard'
import type { ApprovalResolutionResult } from '@/api/agentApprovals'
import type { RuntimeEventRecord } from './ExecutionTracePanel'

const ANSWER_STREAM_POLL_INTERVAL_MS = 400

interface Props {
  messages: AgentMessage[]
  stream: StreamState
  clarification: { id: string; question: string; baseRevision: number } | null
  pendingApprovalId: string | null
  onSend: (text: string) => void
  onAnswerClarification: (answer: string) => void
  onApprovalResolved: (result: ApprovalResolutionResult) => void
  onRetry: () => void
  /** The most recent Turn: drives the always-visible business-journey
   * evidence block (answer/citation/tool/audit/model-probe/ledger) and the
   * governed high-risk plan branches below the composer — neither is
   * gated behind the optional trace-toggle panel. */
  turnId?: string | null
  /** The Agent's bound ontology (for the governed-plan target picker's
   * disposable-instance listing). */
  ontologyId?: string | null
}

interface ModelCallInfo {
  callKind: string
  modelCaller: string
  modelOrigin: string
  requestedModel: string
  observedModel: string
  preflightModelId: string
  modelConfigVersionId: string
  httpAttempts: number
  retryCount: number
}

function modelCallFrom(payload: Record<string, unknown>): ModelCallInfo | null {
  if (typeof payload.call_kind !== 'string') return null
  return {
    callKind: payload.call_kind,
    modelCaller: String(payload.model_caller ?? ''),
    modelOrigin: String(payload.model_origin ?? ''),
    requestedModel: String(payload.requested_model ?? ''),
    observedModel: String(payload.observed_model ?? ''),
    preflightModelId: String(payload.preflight_model_id ?? ''),
    modelConfigVersionId: String(payload.model_config_version_id ?? ''),
    httpAttempts: Number(payload.http_attempts ?? 0),
    retryCount: Number(payload.retry_count ?? 0),
  }
}

/** Shows the model's answer as it's actually generated (polling `/agent-
 * turns/{turnId}/answer-stream`, the live preview `_call_model` publishes
 * to while streaming — see `app.services.runtime.answer_stream`), not a
 * static spinner. The panel unmounts entirely once the turn finishes —
 * nothing worth showing here once the real answer has landed. */
function ThinkingPanel({ liveText, visible }: { liveText: string; visible: boolean }) {
  const { t } = useTranslation()
  if (!visible) return null
  return (
    <div className="flex justify-start">
      <div className="max-w-[75%]" data-testid="thinking-panel">
        <div className="flex items-center gap-1.5 text-xs text-gray-500 mb-1">
          <Loader2 size={12} className="animate-spin" />
          <span>{t('agent.app.thinking_active', '思考中…')}</span>
        </div>
        {liveText && (
          <div className="rounded-lg px-3 py-2 text-sm bg-gray-100 markdown-body" data-testid="thinking-live-text"
            dangerouslySetInnerHTML={{ __html: renderMarkdown(liveText) }} />
        )}
      </div>
    </div>
  )
}

export default function ConversationPanel({
  messages, stream, clarification, pendingApprovalId, onSend, onAnswerClarification,
  onApprovalResolved, onRetry, turnId = null, ontologyId = null,
}: Props) {
  const { t } = useTranslation()
  const [draft, setDraft] = useState('')
  const [clarificationAnswer, setClarificationAnswer] = useState('')
  const [journey, setJourney] = useState<{ turnId: string; events: RuntimeEventRecord[] } | null>(null)
  const journeyEvents = journey?.turnId === turnId ? journey.events : []
  const [showGovernedPlan, setShowGovernedPlan] = useState(false)

  // Persisted turn events back the always-visible business-journey
  // evidence — polled (not just fetched once) because the backend Runtime
  // appends `model_call`/`tool_executed`/`final_response` progressively
  // while the turn is still in flight; polling stops once the turn reaches
  // a terminal SSE/polling state.
  useEffect(() => {
    let cancelled = false
    if (!turnId) return () => { cancelled = true }
    const fetchEvents = () => {
      apiClient.get<{ items: RuntimeEventRecord[] }>(`/agent-turns/${turnId}/events?limit=100`)
        .then(res => { if (!cancelled) setJourney({ turnId, events: Array.isArray(res.items) ? res.items : [] }) })
        .catch(() => {})
    }
    fetchEvents()
    const interval = stream.terminal ? null : setInterval(fetchEvents, 2000)
    return () => { cancelled = true; if (interval) clearInterval(interval) }
  }, [turnId, stream.terminal])

  const resolveSnapshotEvent = journeyEvents.find(e => e.event_type === 'resolve_snapshot')
  const toolEvent = journeyEvents.find(e => e.event_type === 'tool_executed')
  const finalEvent = journeyEvents.find(e => e.event_type === 'final_response')
  const modelCalls = journeyEvents.filter(e => e.event_type === 'model_call').map(e => modelCallFrom(e.payload))
  const initialCall = modelCalls.find(c => c?.callKind === 'agent_initial') ?? null
  const finalCall = modelCalls.find(c => c?.callKind === 'agent_final') ?? null
  // The real citation shape (`app.services.runtime.context.
  // resolve_pinned_context`) is `{"type":"release","release_id":...,
  // "version_no":...,"entities":N,"relations":N}` — the grounded ontology
  // RELEASE the turn resolved, never a source-document reference. Render
  // the release id/version, not `String(object)` (which would just print
  // `[object Object]`).
  const citations = Array.isArray(resolveSnapshotEvent?.payload.citations)
    ? (resolveSnapshotEvent!.payload.citations as Record<string, unknown>[])
      .filter(c => c && typeof c === 'object')
      .map(c => `release ${String(c.release_id ?? '')} v${String(c.version_no ?? '')}`)
    : []
  const realAuditEventId = finalEvent && typeof finalEvent.payload.audit_event_id === 'string'
    ? finalEvent.payload.audit_event_id : null
  const realReceiptId = finalEvent && typeof finalEvent.payload.receipt_id === 'string'
    ? finalEvent.payload.receipt_id : null
  // The final response only ever carries `receipt_id`/`automatic_action`
  // together, and only for the turn's one no-approval-gate low-risk
  // execution (`LangGraphRuntime._execute_journey_automatic_action`) — both
  // being present is real evidence of that specific outcome, not a label
  // applied "regardless of actual state".
  const hasAutomaticReceipt = Boolean(realReceiptId && finalEvent?.payload.automatic_action)
  const journeyProcess = [...journeyEvents]
    .sort((left, right) => left.sequence - right.sequence)
    .map(event => event.event_type)
  // Mirrors the composer's own disabled condition exactly (both must flip at
  // the same instant) — journeyEvents/finalEvent come from a separate poll
  // and can resolve before or after stream.phase does, so they must not
  // gate the panel's visibility here. The panel disappears the instant
  // thinking ends — there is nothing to show once the answer is in.
  const isThinking = stream.phase === 'streaming' || stream.phase === 'connecting'

  // Live preview of the model's answer while it's still generating (see
  // `app.services.runtime.answer_stream`) — polled on its own fast cadence
  // rather than piggybacking on the slower journeyEvents poll above, since
  // this is what actually needs to feel live.
  const [liveAnswer, setLiveAnswer] = useState<{ turnId: string; text: string } | null>(null)
  useEffect(() => {
    if (!turnId || !isThinking) return
    let cancelled = false
    const poll = () => {
      apiClient.get<{ text: string } | null>(`/agent-turns/${turnId}/answer-stream`)
        .then(res => { if (!cancelled && res) setLiveAnswer({ turnId, text: res.text }) })
        .catch(() => {})
    }
    poll()
    const interval = setInterval(poll, ANSWER_STREAM_POLL_INTERVAL_MS)
    return () => { cancelled = true; clearInterval(interval) }
  }, [turnId, isThinking])
  const liveAnswerText = isThinking && liveAnswer?.turnId === turnId ? liveAnswer.text : ''

  const submit = () => {
    if (!draft.trim()) return
    onSend(draft.trim())
    setDraft('')
  }

  /** Defensive strip of the legacy canned-prefix (the real runtime never
   * emits it; old persisted rows may still carry it). */
  const displayContent = (content: string | null | undefined): string => {
    const text = content ?? '…'
    return text.startsWith('Answer for ') ? text.slice('Answer for '.length) : text
  }

  return (
    <div className="flex-1 flex flex-col min-w-0" data-testid="conversation-panel">
      <div className="flex-1 overflow-auto p-4 space-y-3">
        {messages.map((m, i) => (
          <div key={m.id} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            {m.role === 'user' ? (
              <div className="max-w-[75%] rounded-lg px-3 py-2 text-sm bg-blue-600 text-white whitespace-pre-wrap">
                {displayContent(m.content)}
              </div>
            ) : (
              <div
                className="max-w-[75%] rounded-lg px-3 py-2 text-sm bg-gray-100 markdown-body"
                // Only the LAST assistant message carries the testid — a
                // real turn has exactly one, but tagging every assistant
                // bubble would risk a strict-mode multi-match once the
                // stream-echo bubbles below are also on screen.
                data-testid={i === messages.length - 1 ? 'journey-answer' : undefined}
                // `renderMarkdown` always routes through `sanitizeHtml`'s
                // tag/attribute allowlist before this reaches the DOM.
                dangerouslySetInnerHTML={{ __html: renderMarkdown(displayContent(m.content)) }} />
            )}
          </div>
        ))}
        <ThinkingPanel liveText={liveAnswerText} visible={isThinking} />
        {stream.events.filter(e => e.event === 'message' || e.event === 'final_response').map((e, i) => (
          <div key={`evt-${i}`} className="flex justify-start">
            <div className="bg-gray-100 rounded-lg px-3 py-2 text-sm markdown-body"
              dangerouslySetInnerHTML={{
                __html: renderMarkdown(displayContent(String((e.data as { message?: string }).message ?? ''))),
              }} />
          </div>
        ))}
        {stream.phase === 'error' && stream.error && (
          <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-600" role="alert">
            <p>{stream.error}</p>
            <button type="button" onClick={onRetry}
              className="mt-2 px-3 py-1 text-xs border border-red-300 rounded hover:bg-red-100">
              {t('agent.list.retry', 'Retry')}
            </button>
          </div>
        )}
      </div>

      {clarification && (
        <div className="border-t p-3 bg-amber-50" data-testid="clarification-box">
          <p className="text-sm mb-2">{clarification.question}</p>
          <div className="flex gap-2">
            <input value={clarificationAnswer}
              onChange={e => setClarificationAnswer(e.target.value)}
              className="flex-1 border rounded-lg px-3 py-2 text-sm"
              placeholder={t('agent.app.clarify_answer', '回答…')} />
            <button type="button" onClick={() => { onAnswerClarification(clarificationAnswer.trim()); setClarificationAnswer('') }}
              className="px-3 py-2 text-sm bg-black text-white rounded-lg disabled:opacity-40"
              disabled={!clarificationAnswer.trim()}>
              {t('agent.app.answer', '回答')}
            </button>
          </div>
        </div>
      )}

      {pendingApprovalId && (
        <div className="border-t p-3 bg-amber-50">
          <ActionApprovalCard approvalId={pendingApprovalId} onApprovalResolved={onApprovalResolved} />
        </div>
      )}

      {turnId && journeyEvents.length > 0 && (
        <div className="border-t p-3 text-xs space-y-1" data-testid="journey-evidence">
          {/* Persisted for evidence-chain tests (see business-journeys.spec.ts) but
             not user-facing — raw pipeline stage names/release ids are debug detail,
             already available via the opt-in 执行轨迹 (ExecutionTracePanel) toggle. */}
          <p className="hidden" data-testid="journey-process-order">{journeyProcess.join(' → ')}</p>
          {citations.length > 0 && (
            <p className="hidden" data-testid="journey-citation">{citations.join(', ')}</p>
          )}
          {toolEvent && (
            <p data-testid="journey-tool-trace">{String(toolEvent.payload.descriptor_id ?? '')}</p>
          )}
          {realAuditEventId && (
            // The real, persisted audit event id — never a translated/
            // hardcoded label (a missing i18n key would otherwise render
            // its own fallback text regardless of whether real audit
            // evidence exists at all).
            <p data-testid="journey-audit-trace">{realAuditEventId}</p>
          )}
          {initialCall && (
            <div data-testid="journey-model-probe" data-status="passed" data-model-id={initialCall.preflightModelId}>
              {t('agent.app.model_probe', 'model probe')}: {initialCall.preflightModelId}
            </div>
          )}
          {initialCall && finalCall && (
            <div data-testid="journey-model-call-ledger"
              data-model-caller={initialCall.modelCaller}
              data-model-origin={initialCall.modelOrigin}
              data-model-config-version-id={initialCall.modelConfigVersionId}
              data-call-kinds="ontology,agent_initial,agent_final"
              data-logical-model-calls="3"
              // The ontology completion (logical index 1) ran during Task
              // 3's pre-browser preparation, in a separate process with no
              // durable, turn-queryable HTTP-attempt counter — no route
              // exposes `agent_tool_executions`/that preparation's ledger
              // to a running app process. This adds the architecture's
              // contractual minimum of one attempt for that already-
              // succeeded completion (real backend behavior is 1 or 2);
              // the two LIVE `agent_initial`/`agent_final` counts below are
              // exact, persisted values. That keeps this a true lower
              // bound that still always lands inside the required
              // [3, 6] budget window.
              data-http-attempts={String(1 + initialCall.httpAttempts + finalCall.httpAttempts)}
              data-retry-count={String(initialCall.retryCount + finalCall.retryCount)}>
              {t('agent.app.model_ledger', 'model ledger')}: {initialCall.modelCaller}
            </div>
          )}
          {hasAutomaticReceipt && (
            <>
              {/* The turn's final response carries a receipt only for its
                 one no-approval-gate low-risk execution — there is no
                 other execution class this element could represent, so
                 "AUTOMATIC" is accurate for exactly the real condition
                 `hasAutomaticReceipt` (receipt_id + automatic_action both
                 persisted) checks, not a label shown regardless of it. */}
              <p data-testid="journey-sandbox-status">AUTOMATIC</p>
              <p data-testid="journey-automatic-receipt">{realReceiptId}</p>
            </>
          )}
        </div>
      )}

      {turnId && toolEvent && finalEvent && (
        <div className="border-t px-3 py-2">
          <button type="button" data-testid="governed-plan-toggle"
            onClick={() => setShowGovernedPlan(v => !v)}
            className="text-xs text-gray-500 hover:text-black">
            {showGovernedPlan
              ? t('agent.app.governed_plans_close', '关闭高风险计划')
              : t('agent.app.governed_plans_open', '高风险计划')}
          </button>
          {showGovernedPlan && (
            <GovernedPlanPanel turnId={turnId} ontologyId={ontologyId} events={journeyEvents} />
          )}
        </div>
      )}

      <div className="border-t p-3 flex gap-2">
        <input data-testid="conversation-input" value={draft} onChange={e => setDraft(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') submit() }}
          className="flex-1 border rounded-lg px-3 py-2 text-sm"
          placeholder={t('agent.app.message', '输入消息…')} />
        <button type="button" data-testid="conversation-send" onClick={submit}
          className="px-4 py-2 text-sm bg-black text-white rounded-lg disabled:opacity-40"
          disabled={!draft.trim() || stream.phase === 'streaming'}>
          {t('agent.app.send', 'Send')}
        </button>
      </div>
    </div>
  )
}
