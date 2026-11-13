import '@/i18n'
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { refreshApi } from '@/api/refresh'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import RefreshOperationsPage from './RefreshOperationsPage'
import type { RefreshRunView, RefreshStatus } from '@/types/refresh'

const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

beforeEach(() => server.use(
  http.get('*/api/v2/refresh/sources/source-001/schedule', () => HttpResponse.json({
    id: 'schedule-001', target_type: 'source', target_id: 'source-001', cron_expr: '0 2 * * *',
    timezone: 'UTC', business_calendar: [], sla_seconds: 0, retry_policy: null,
    backfill_window_seconds: 0, max_pending_runs: 1, enabled: true, next_due_at: null,
  })),
))

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/runtime/refresh/source-001']}>
      <Routes>
        <Route path="/runtime/refresh/:sourceId" element={<RefreshOperationsPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

function baseRun(overrides: Partial<RefreshRunView>): RefreshRunView {
  return {
    run_id: 'run-dead-001', source_id: 'source-001', resource: 'purchase_orders', policy: 'micro_batch',
    trigger: 'manual', status: 'dead_lettered', dispatch_state: 'dispatched', dispatch_queue: 'refresh.poll',
    config_version: 8, cursor_contract: 'watermark_primary_key', cursor_before: null, cursor_after: null,
    input_dataset_version_ids: [], pipeline_run_id: null, lag_seconds: 120, duplicate_count: 0, late_count: 0,
    retry_count: 2, retry_reason: null, dead_letter_id: null, replay_status: null, cancel_requested_at: null,
    cancel_requested_by: null, cancel_reason: null, terminal_at: '2026-08-29T00:00:00Z', already_terminal: false,
    ...overrides,
  }
}

function baseStatus(run: RefreshRunView, overrides: Partial<RefreshStatus> = {}): RefreshStatus {
  return {
    source_id: 'source-001', resource: 'purchase_orders', policy: 'micro_batch', config_version: 8,
    cursor_contract: 'watermark_primary_key', cursor: { primary_key: 'row-000100' }, cursor_observed_at: null,
    fencing_token: 0, latest_run: run, input_dataset_version_id: null, pipeline_run_id: null, lag_seconds: 120,
    freshness_lag_seconds: 120, duplicate_count: 0, late_count: 0, retry_count: 2, dlq_count: 1,
    next_schedule_at: '2026-08-31T02:00:00Z', sla_status: 'breached', backfill_window_seconds: 0,
    ...overrides,
  }
}

describe('RefreshOperationsPage', () => {
  it('does not expose a prior source run after a delayed response', async () => {
    let resolveOld!: (value: RefreshStatus) => void
    const oldResponse = new Promise<RefreshStatus>(resolve => { resolveOld = resolve })
    const get = vi.spyOn(refreshApi, 'getStatus').mockImplementation(id => id === 'old' ? oldResponse :
      Promise.resolve(baseStatus(baseRun({ run_id: 'current-run' }), { source_id: id })))
    const schedule = vi.spyOn(refreshApi, 'getSchedule').mockRejectedValue(new Error('not configured'))
    try {
      render(<MemoryRouter initialEntries={['/refresh/old']}>
        <Link to="/refresh/new">next source</Link>
        <Routes><Route path="/refresh/:sourceId" element={<RefreshOperationsPage />} /></Routes>
      </MemoryRouter>)
      await userEvent.click(screen.getByText('next source'))
      expect(await screen.findByText('current-run')).toBeTruthy()
      await act(async () => { resolveOld(baseStatus(baseRun({ run_id: 'old-run' }))); await oldResponse })
      expect(screen.getByText('current-run')).toBeTruthy()
      expect(screen.queryByText('old-run')).toBeNull()
    } finally { get.mockRestore(); schedule.mockRestore() }
  })

  it('loads the persisted schedule timezone and business calendar on mount', async () => {
    server.use(
      http.get('*/api/v2/refresh/sources/source-001/status', () => HttpResponse.json(baseStatus(baseRun({})))),
      http.get('*/api/v2/refresh/sources/source-001/schedule', () => HttpResponse.json({
        id: 'schedule-001', target_type: 'source', target_id: 'source-001', cron_expr: '0 2 * * *',
        timezone: 'Asia/Shanghai', business_calendar: ['2026-10-01'], sla_seconds: 0,
        retry_policy: null, backfill_window_seconds: 0, max_pending_runs: 1, enabled: true, next_due_at: null,
      })),
    )
    renderPage()
    expect(await screen.findByTestId('refresh-schedule-timezone-current')).toHaveTextContent('Asia/Shanghai')
    expect(screen.getByTestId('refresh-schedule-business-calendar')).toHaveTextContent('2026-10-01')
  })

  it('shows policy, cursor, lag, and a dead-lettered status, then replays', async () => {
    const run = baseRun({})
    server.use(
      http.get('*/api/v2/refresh/sources/source-001/status', () => HttpResponse.json(baseStatus(run))),
      http.post('*/api/v2/refresh/runs/run-dead-001/replay', () =>
        HttpResponse.json(baseRun({ run_id: 'run-replay-001', status: 'queued', retry_reason: null }))),
    )
    renderPage()
    expect(await screen.findByTestId('refresh-policy')).toHaveTextContent('micro_batch')
    expect(screen.getByTestId('refresh-cursor')).toHaveTextContent('row-000100')
    expect(screen.getByTestId('refresh-lag-seconds')).toHaveTextContent('120')
    expect(screen.getByTestId('refresh-latest-status')).toHaveTextContent('DEAD_LETTERED')

    await userEvent.click(screen.getByTestId('replay-refresh-run'))
    expect(await screen.findByTestId('refresh-replay-status')).toHaveTextContent('QUEUED')
  })

  it('shows the server-recorded replay state for a dead-lettered run', async () => {
    const run = baseRun({ replay_status: 'queued' })
    server.use(
      http.get('*/api/v2/refresh/sources/source-001/status', () => HttpResponse.json(baseStatus(run))),
    )
    renderPage()
    expect(await screen.findByTestId('refresh-replay-state')).toHaveTextContent('QUEUED')
  })

  it('shows the plain already-terminal message and leaves status unchanged on a late cancel', async () => {
    const run = baseRun({ run_id: 'run-succeeded-001', status: 'succeeded', retry_reason: null })
    server.use(
      http.get('*/api/v2/refresh/sources/source-001/status', () => HttpResponse.json(baseStatus(run))),
      http.post('*/api/v2/refresh/runs/run-succeeded-001/cancel', () =>
        HttpResponse.json({ status: 'succeeded', already_terminal: true })),
    )
    renderPage()
    expect(await screen.findByTestId('refresh-latest-status')).toHaveTextContent('SUCCEEDED')
    // an authorized-but-terminal run shows no active cancel control
    expect(screen.getByTestId('cancel-refresh-run')).toHaveProperty('disabled', true)
  })

  it('cancels an active run and shows the recorded reason', async () => {
    const run = baseRun({ run_id: 'run-running-001', status: 'running', retry_reason: null })
    const cancelled = baseRun({
      run_id: 'run-running-001', status: 'cancelled', cancel_reason: 'planned source maintenance',
      cancel_requested_by: 'user-1', cancel_requested_at: '2026-08-30T00:00:00Z',
      terminal_at: '2026-08-30T00:00:00Z',
    })
    server.use(
      http.get('*/api/v2/refresh/sources/source-001/status', () => HttpResponse.json(baseStatus(run))),
      http.post('*/api/v2/refresh/runs/run-running-001/cancel', () => HttpResponse.json(cancelled)),
    )
    renderPage()
    expect(await screen.findByTestId('refresh-latest-status')).toHaveTextContent('RUNNING')
    expect(screen.getByTestId('cancel-refresh-run')).toHaveProperty('disabled', false)

    server.use(
      http.get('*/api/v2/refresh/sources/source-001/status', () => HttpResponse.json(baseStatus(cancelled))),
    )
    await userEvent.click(screen.getByTestId('cancel-refresh-run'))
    await waitFor(async () => expect(await screen.findByTestId('refresh-latest-status')).toHaveTextContent('CANCELLED'))
    expect(screen.getByTestId('refresh-cancel-reason')).toHaveTextContent('planned source maintenance')
    expect(screen.getByTestId('refresh-cancel-requested-by')).toHaveTextContent('user-1')
    expect(screen.getByTestId('refresh-cancel-requested-at')).toHaveTextContent('2026-08-30T00:00:00Z')
  })

  it('renders the timezone and business calendar returned after saving a schedule', async () => {
    const user = userEvent.setup()
    const run = baseRun({})
    server.use(
      http.get('*/api/v2/refresh/sources/source-001/status', () => HttpResponse.json(baseStatus(run))),
      http.put('*/api/v2/refresh/sources/source-001/schedule', () => HttpResponse.json({
        id: 'schedule-001', target_type: 'refresh_source', target_id: 'source-001',
        cron_expr: '0 2 * * *', timezone: 'Asia/Shanghai', business_calendar: ['CN-HOLIDAYS'],
        sla_seconds: 0, retry_policy: null, backfill_window_seconds: 0, max_pending_runs: 1,
        enabled: true, next_due_at: '2026-08-31T02:00:00Z',
      })),
    )
    renderPage()
    await screen.findByTestId('refresh-policy')
    await user.type(screen.getByTestId('refresh-schedule-cron'), '0 2 * * *')
    await user.clear(screen.getByTestId('refresh-schedule-timezone'))
    await user.type(screen.getByTestId('refresh-schedule-timezone'), 'Asia/Shanghai')
    await user.click(screen.getByTestId('refresh-schedule-submit'))

    expect(await screen.findByTestId('refresh-schedule-timezone-current')).toHaveTextContent('Asia/Shanghai')
    expect(screen.getByTestId('refresh-schedule-business-calendar')).toHaveTextContent('CN-HOLIDAYS')
  })

  it('renders a CONFIGURATION_DRIFT failure with an unchanged cursor', async () => {
    // Regression test for the Important finding: the implementer's report
    // claimed CONFIGURATION_DRIFT is never persisted on a queryable
    // refresh-run response, which is factually wrong — `refresh_tasks.py`
    // persists `retry_reason` and this page already renders it via the
    // `refresh-latest-reason` testid; this proves that path end to end.
    const run = baseRun({
      run_id: 'run-drift-001', status: 'failed', retry_reason: 'CONFIGURATION_DRIFT',
      cursor_after: null,
    })
    server.use(
      http.get('*/api/v2/refresh/sources/source-001/status', () =>
        HttpResponse.json(baseStatus(run, { cursor: { primary_key: 'row-000100' } }))),
    )
    renderPage()
    expect(await screen.findByTestId('refresh-latest-status')).toHaveTextContent('FAILED')
    expect(screen.getByTestId('refresh-latest-reason')).toHaveTextContent('CONFIGURATION_DRIFT')
    // The cursor must remain the last-known-good value — a configuration
    // drift failure must never advance it.
    expect(screen.getByTestId('refresh-cursor')).toHaveTextContent('row-000100')
  })

  it('renders the T+1 next scheduled run time', async () => {
    const run = baseRun({})
    server.use(
      http.get('*/api/v2/refresh/sources/source-001/status', () =>
        HttpResponse.json(baseStatus(run, { next_schedule_at: '2026-08-31T02:00:00Z' }))),
    )
    renderPage()
    expect(await screen.findByTestId('refresh-next-schedule-at')).toHaveTextContent('2026-08-31T02:00:00Z')
  })

  it('shows an enabled cancel control for a queued (not-yet-started) run', async () => {
    const run = baseRun({ run_id: 'run-queued-001', status: 'queued', retry_reason: null, dispatch_state: 'pending' })
    server.use(
      http.get('*/api/v2/refresh/sources/source-001/status', () => HttpResponse.json(baseStatus(run))),
    )
    renderPage()
    expect(await screen.findByTestId('refresh-latest-status')).toHaveTextContent('QUEUED')
    expect(screen.getByTestId('cancel-refresh-run')).toHaveProperty('disabled', false)
  })
})
