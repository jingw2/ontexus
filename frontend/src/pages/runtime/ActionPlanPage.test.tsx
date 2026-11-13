import '@/i18n'
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { runtimeApi } from '@/api/runtime'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import ActionPlanPage from './ActionPlanPage'

const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const PLAN = {
  id: 'plan-hitl-001',
  semantic_snapshot_id: 'snap-valid-001',
  ontology_release_id: 'release-001',
  agent_id: 'agent-1',
  user_id: 'user-1',
  action_id: 'action-1',
  input_facts: {},
  evidence_citations: [],
  rule_outcomes: [],
  managed_action_binding_id: 'binding-1',
  binding_version: '1',
  parameters: {},
  target_key: ['id', '42'],
  before_image_hash: 'a'.repeat(64),
  version_hash: 'b'.repeat(64),
  predicted_diff: { status: { before: 'pending', after: 'approved' } },
  impact_scope: { rows: 1 },
  risk_classification: 'human_approved',
  policy_decision: { requires_hitl: true },
  precondition_hashes: ['c'.repeat(64)],
  expiry: '2026-09-01T00:00:00Z',
  idempotency_key: 'idem-1',
  plan_hash: 'plan-hash-hitl-001',
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/runtime/action-plans/plan-hitl-001']}>
      <Routes>
        <Route path="/runtime/action-plans/:planId" element={<ActionPlanPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ActionPlanPage', () => {
  it('does not replace the current plan with a late prior-route response', async () => {
    type Plan = Awaited<ReturnType<typeof runtimeApi.getActionPlan>>
    let resolveOld!: (value: Plan) => void
    const oldResponse = new Promise<Plan>(resolve => { resolveOld = resolve })
    const get = vi.spyOn(runtimeApi, 'getActionPlan').mockImplementation(id =>
      id === 'old' ? oldResponse : Promise.resolve({ ...PLAN, id, plan_hash: 'new-hash' } as Plan))
    const status = vi.spyOn(runtimeApi, 'getExecutionStatus').mockResolvedValue({ status: 'not_started', plan_id: 'new', correlation_id: 'test' })
    try {
      render(<MemoryRouter initialEntries={['/plans/old']}>
        <Link to="/plans/new">next plan</Link>
        <Routes><Route path="/plans/:planId" element={<ActionPlanPage />} /></Routes>
      </MemoryRouter>)
      await userEvent.click(screen.getByText('next plan'))
      expect(await screen.findByTestId('plan-hash')).toHaveTextContent('new-hash')
      await act(async () => { resolveOld(PLAN as Plan); await oldResponse })
      expect(screen.getByTestId('plan-hash')).toHaveTextContent('new-hash')
    } finally { get.mockRestore(); status.mockRestore() }
  })

  it('shows the exact plan hash, approves, then executes and shows the receipt', async () => {
    let approveBody: unknown = null
    let executeBody: unknown = null
    server.use(
      http.get('*/api/v2/runtime/action-plans/plan-hitl-001', () => HttpResponse.json(PLAN)),
      http.get('*/api/v2/runtime/execution-status/plan-hitl-001', () =>
        HttpResponse.json({ plan_id: 'plan-hitl-001', status: 'not_started', correlation_id: 'corr-0' })),
      http.post('*/api/v2/runtime/action-plans/plan-hitl-001/approve', async ({ request }) => {
        approveBody = await request.json()
        return HttpResponse.json({
          plan_id: 'plan-hitl-001', plan_hash: 'plan-hash-hitl-001', approver_agent_id: 'agent-1',
          approver_user_id: 'user-1', approved_at: '2026-08-30T00:00:00Z', execution_class: 'human_approved',
          expiry: '2026-09-01T00:00:00Z', correlation_id: 'corr-1',
        })
      }),
      http.post('*/api/v2/runtime/action-plans/plan-hitl-001/execute', async ({ request }) => {
        executeBody = await request.json()
        return HttpResponse.json({
          execution_id: 'execution-001', plan_id: 'plan-hitl-001', plan_hash: 'plan-hash-hitl-001',
          status: 'succeeded', execution_class: 'human_approved', dialect: 'postgresql',
          writer_receipt: { row_version: 2 }, audit_id: 'audit-1', idempotency_key: 'idem-1',
          reconciliation_case_id: null,
        })
      }),
    )

    renderPage()
    expect(await screen.findByTestId('plan-hash')).toHaveTextContent('plan-hash-hitl-001')

    await userEvent.click(screen.getByTestId('approve-exact-plan'))
    await waitFor(() => expect(approveBody).toEqual({ plan_hash: 'plan-hash-hitl-001' }))
    expect(await screen.findByTestId('approval-receipt')).toBeTruthy()

    await userEvent.click(screen.getByTestId('execute-exact-plan'))
    await waitFor(() => expect(executeBody).toEqual({ plan_hash: 'plan-hash-hitl-001' }))
    expect(await screen.findByTestId('execution-status')).toHaveTextContent('SUCCEEDED')
  })

  it('surfaces a denied approval without crashing', async () => {
    server.use(
      http.get('*/api/v2/runtime/action-plans/plan-hitl-001', () => HttpResponse.json(PLAN)),
      http.get('*/api/v2/runtime/execution-status/plan-hitl-001', () =>
        HttpResponse.json({ plan_id: 'plan-hitl-001', status: 'not_started', correlation_id: 'corr-0' })),
      http.post('*/api/v2/runtime/action-plans/plan-hitl-001/approve', () =>
        HttpResponse.json({ decision: 'DENY', reason_code: 'INVALID_PLAN_HASH', correlation_id: null, semantic_snapshot_id: null, ontology_release_id: null, result: null }, { status: 409 })),
    )
    renderPage()
    await screen.findByTestId('plan-hash')
    await userEvent.click(screen.getByTestId('approve-exact-plan'))
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.queryByTestId('approval-receipt')).toBeNull()
  })
})
