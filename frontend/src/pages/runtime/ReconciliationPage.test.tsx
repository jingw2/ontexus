import '@/i18n'
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { runtimeApi } from '@/api/runtime'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import ReconciliationPage from './ReconciliationPage'

// Zero-coverage gap identified by review: this page had no test file at
// all, including no test proving the "create rollback plan" flow or the
// PROPOSAL_ONLY-style state the brief describes.

const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/runtime/reconciliations/recon-001']}>
      <Routes>
        <Route path="/runtime/reconciliations/:reconciliationId" element={<ReconciliationPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

const UNKNOWN_CASE = {
  id: 'recon-001',
  execution_id: 'exec-001',
  plan_id: 'plan-001',
  status: 'UNKNOWN' as const,
  unknown_reason: 'writer_timeout',
  observed_effect: { rows_observed: 0 },
  next_action: 'manual_review',
  created_at: '2026-08-30T00:00:00Z',
}

const ROLLBACK_PLAN = {
  id: 'plan-rollback-001',
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
  predicted_diff: {},
  impact_scope: {},
  risk_classification: 'human_approved',
  policy_decision: {},
  precondition_hashes: [],
  expiry: '2026-09-01T00:00:00Z',
  idempotency_key: 'idem-rollback-1',
  plan_hash: 'plan-hash-rollback-001',
}

describe('ReconciliationPage', () => {
  it('does not show a late case response after navigating to another case', async () => {
    let resolveOld!: (value: typeof UNKNOWN_CASE) => void
    const oldResponse = new Promise<typeof UNKNOWN_CASE>(resolve => { resolveOld = resolve })
    const get = vi.spyOn(runtimeApi, 'getReconciliation').mockImplementation(id =>
      id === 'recon-001' ? oldResponse : Promise.resolve({ ...UNKNOWN_CASE, id, execution_id: 'exec-002' }))
    try {
      render(<MemoryRouter initialEntries={['/cases/recon-001']}>
        <Link to="/cases/recon-002">next case</Link>
        <Routes><Route path="/cases/:reconciliationId" element={<ReconciliationPage />} /></Routes>
      </MemoryRouter>)
      await userEvent.click(screen.getByText('next case'))
      expect(await screen.findByText('exec-002')).toBeTruthy()
      await act(async () => { resolveOld(UNKNOWN_CASE); await oldResponse })
      expect(screen.getByText('exec-002')).toBeTruthy()
      expect(screen.queryByText('exec-001')).toBeNull()
    } finally { get.mockRestore() }
  })

  it('renders an UNKNOWN case with its unknown reason and next action', async () => {
    server.use(
      http.get('*/api/v2/runtime/reconciliations/recon-001', () => HttpResponse.json(UNKNOWN_CASE)),
    )
    renderPage()

    expect(await screen.findByTestId('reconciliation-status')).toHaveTextContent('UNKNOWN')
    expect(screen.getByTestId('reconciliation-unknown-reason')).toHaveTextContent('writer_timeout')
    expect(screen.getByTestId('reconciliation-next-action')).toHaveTextContent('manual_review')
    expect(screen.getByTestId('reconciliation-observed-effect')).toHaveTextContent('rows_observed')
  })

  it('creates a rollback plan and renders it as PROPOSAL_ONLY, never an already-applied write', async () => {
    server.use(
      http.get('*/api/v2/runtime/reconciliations/recon-001', () => HttpResponse.json(UNKNOWN_CASE)),
      http.post('*/api/v2/runtime/executions/exec-001/rollback-plans', () => HttpResponse.json(ROLLBACK_PLAN)),
    )
    renderPage()
    await screen.findByTestId('reconciliation-status')

    await userEvent.click(screen.getByTestId('create-rollback-plan'))

    expect(await screen.findByTestId('rollback-plan-result')).toBeTruthy()
    expect(screen.getByTestId('rollback-plan-hash')).toHaveTextContent('plan-hash-rollback-001')
    expect(screen.getByTestId('rollback-execution-state')).toHaveTextContent('PROPOSAL_ONLY')
  })

  it('shows a denial message when rollback plan creation is denied', async () => {
    server.use(
      http.get('*/api/v2/runtime/reconciliations/recon-001', () => HttpResponse.json(UNKNOWN_CASE)),
      http.post('*/api/v2/runtime/executions/exec-001/rollback-plans', () =>
        HttpResponse.json({ decision: 'DENY', reason_code: 'POLICY_DENIED' }, { status: 403 })),
    )
    renderPage()
    await screen.findByTestId('reconciliation-status')

    await userEvent.click(screen.getByTestId('create-rollback-plan'))

    expect(await screen.findByRole('alert')).toHaveTextContent('回滚计划创建被拒绝')
    expect(screen.queryByTestId('rollback-plan-result')).toBeNull()
  })
})
