import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useParams } from 'react-router-dom'
import { refreshApi } from '@/api/refresh'
import type { RefreshRunView, RefreshScheduleView, RefreshStatus } from '@/types/refresh'

const ACTIVE_STATUSES = new Set(['queued', 'running', 'cancel_requested'])
const REPLAYABLE_STATUSES = new Set(['failed', 'dead_lettered'])

/** Enum-style workflow status tokens are rendered upper-case (matching the
 * existing `fmt.toUpperCase()` convention for short enum-like tokens in
 * `InfoTab.tsx`'s export-format buttons) — config/policy values (e.g.
 * `micro_batch`) are left exactly as the server returns them. */
const upper = (s: string) => s.toUpperCase()

function cursorLabel(cursor: Record<string, unknown> | null | undefined): string {
  if (!cursor) return '—'
  if (typeof cursor.primary_key === 'string') return cursor.primary_key
  if (typeof cursor.opaque_value === 'string') return cursor.opaque_value
  return JSON.stringify(cursor)
}

/**
 * Task 27: refresh schedule/cursor/lag/run/DLQ-replay operator surface
 * (`backend/app/routers/v2/refresh.py`, Tasks 6-10). Renders server-owned
 * state only; a cancel control is shown only for an authorized active run
 * and disabled once any durable terminal state is reached. A cancellation
 * that arrives after the run already finished renders the backend's plain
 * `{status, already_terminal: true}` result as an explanatory message —
 * never a distinct error state and never rollback language.
 */
export default function RefreshOperationsPage() {
  const { sourceId } = useParams<{ sourceId: string }>()
  return <RefreshOperationsContent key={sourceId} sourceId={sourceId} />
}

function RefreshOperationsContent({ sourceId }: { sourceId?: string }) {
  const { t } = useTranslation()
  const [status, setStatus] = useState<RefreshStatus | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [cancelReason, setCancelReason] = useState('planned source maintenance')
  const [cancelMessage, setCancelMessage] = useState('')
  const [replayRun, setReplayRun] = useState<RefreshRunView | null>(null)
  const [cronExpr, setCronExpr] = useState('')
  const [timezoneName, setTimezoneName] = useState('UTC')
  const [savedSchedule, setSavedSchedule] = useState<RefreshScheduleView | null>(null)

  const load = useCallback(() => {
    if (!sourceId) return
    refreshApi.getStatus(sourceId)
      .then(value => { setStatus(value); setError('') })
      .catch(() => setError(t('runtime.refresh.load_failed', 'Failed to load refresh status')))
  }, [sourceId, t])

  useEffect(() => { void load() }, [load])

  useEffect(() => {
    if (!sourceId) return
    refreshApi.getSchedule(sourceId).then(setSavedSchedule).catch(() => setSavedSchedule(null))
  }, [sourceId])

  const triggerRun = async () => {
    if (!sourceId || !status) return
    setBusy(true)
    setError('')
    try {
      await refreshApi.trigger(sourceId, { mode: status.policy })
      load()
    } catch {
      setError(t('runtime.refresh.trigger_failed', 'Refresh trigger was denied'))
    } finally {
      setBusy(false)
    }
  }

  const cancelRun = async () => {
    const runId = status?.latest_run?.run_id
    if (!runId) return
    setBusy(true)
    setError('')
    setCancelMessage('')
    try {
      const result = await refreshApi.cancel(runId, cancelReason)
      if ('already_terminal' in result && result.already_terminal) {
        setCancelMessage(t('runtime.refresh.already_terminal', 'run already finished; cancellation had no effect'))
      }
      load()
    } catch {
      setError(t('runtime.refresh.cancel_failed', 'Cancellation was denied'))
    } finally {
      setBusy(false)
    }
  }

  const replayRunAction = async () => {
    const run = status?.latest_run
    if (!run) return
    setBusy(true)
    setError('')
    try {
      const created = await refreshApi.replay(run.run_id, run.dead_letter_id ?? undefined)
      setReplayRun(created)
      load()
    } catch {
      setError(t('runtime.refresh.replay_failed', 'Replay was denied'))
    } finally {
      setBusy(false)
    }
  }

  const updateSchedule = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!sourceId || !cronExpr) return
    setBusy(true)
    setError('')
    try {
      const schedule = await refreshApi.setSchedule(sourceId, { cron_expr: cronExpr, timezone: timezoneName, enabled: true })
      setSavedSchedule(schedule)
      load()
    } catch {
      setError(t('runtime.refresh.schedule_failed', 'Schedule update was denied'))
    } finally {
      setBusy(false)
    }
  }

  if (error && !status) {
    return <p className="text-sm text-red-600" role="alert" data-testid="refresh-page-error">{error}</p>
  }
  if (!status) {
    return <div className="p-6 text-gray-400" data-testid="refresh-page-loading">{t('common.loading', 'Loading…')}</div>
  }

  const latest = status.latest_run
  const canCancel = Boolean(latest && ACTIVE_STATUSES.has(latest.status))
  const canReplay = Boolean(latest && REPLAYABLE_STATUSES.has(latest.status))

  return (
    <div data-testid="refresh-operations-page">
      <h2 className="text-base font-medium mb-3">{t('runtime.refresh.title', 'Refresh Operations')} — {sourceId}</h2>

      <dl className="grid grid-cols-2 gap-1 text-sm mb-4">
        <dt className="text-gray-500">{t('runtime.refresh.policy', 'Refresh policy')}</dt>
        <dd data-testid="refresh-policy">{status.policy}</dd>
        <dt className="text-gray-500">{t('runtime.refresh.config_version', 'Source config version')}</dt>
        <dd data-testid="refresh-config-version">{status.config_version}</dd>
        <dt className="text-gray-500">{t('runtime.refresh.cursor_contract', 'Cursor contract')}</dt>
        <dd data-testid="refresh-cursor-contract">{status.cursor_contract}</dd>
        <dt className="text-gray-500">{t('runtime.refresh.cursor', 'Cursor / watermark')}</dt>
        <dd data-testid="refresh-cursor">{cursorLabel(status.cursor)}</dd>
        <dt className="text-gray-500">{t('runtime.refresh.lag_seconds', 'Source lag (seconds)')}</dt>
        <dd data-testid="refresh-lag-seconds">{status.lag_seconds ?? '—'}</dd>
        <dt className="text-gray-500">{t('runtime.refresh.sla_status', 'SLA status')}</dt>
        <dd data-testid="refresh-sla-status">{status.sla_status}</dd>
        <dt className="text-gray-500">{t('runtime.refresh.next_schedule_at', 'Next due (T+1)')}</dt>
        <dd data-testid="refresh-next-schedule-at">{status.next_schedule_at ?? '—'}</dd>
        <dt className="text-gray-500">{t('runtime.refresh.retry_count', 'Retry count')}</dt>
        <dd data-testid="refresh-retry-count">{status.retry_count}</dd>
        <dt className="text-gray-500">{t('runtime.refresh.dlq_count', 'DLQ count')}</dt>
        <dd data-testid="refresh-dlq-count">{status.dlq_count}</dd>
        {status.input_dataset_version_id && (
          <>
            {/* The closest durable "commit marker" a refresh run produces —
                RefreshStatus carries no semantic_snapshot_id of its own. */}
            <dt className="text-gray-500">{t('runtime.refresh.snapshot', 'Produced dataset version')}</dt>
            <dd className="font-mono" data-testid="refresh-snapshot-id">{status.input_dataset_version_id}</dd>
          </>
        )}
      </dl>

      {savedSchedule && (
        <dl className="grid grid-cols-2 gap-1 text-sm mb-4" data-testid="refresh-saved-schedule">
          <dt className="text-gray-500">{t('runtime.refresh.schedule_timezone', 'Schedule timezone')}</dt>
          <dd data-testid="refresh-schedule-timezone-current">{savedSchedule.timezone}</dd>
          <dt className="text-gray-500">{t('runtime.refresh.business_calendar', 'Business calendar')}</dt>
          <dd data-testid="refresh-schedule-business-calendar">
            {savedSchedule.business_calendar.length ? savedSchedule.business_calendar.join(', ') : '—'}
          </dd>
        </dl>
      )}

      {latest && (
        <div className="border rounded-lg p-3 text-sm mb-4">
          <p className="font-medium mb-2">{t('runtime.refresh.latest_run', 'Latest run')} <span className="font-mono text-xs text-gray-400">{latest.run_id}</span></p>
          <div className="grid grid-cols-2 gap-1">
            <span className="text-gray-500">{t('runtime.refresh.latest_status', 'Status')}</span>
            <span data-testid="refresh-latest-status" className="font-medium">{upper(latest.status)}</span>
            {latest.retry_reason && (
              <>
                <span className="text-gray-500">{t('runtime.refresh.latest_reason', 'Latest failure reason')}</span>
                <span data-testid="refresh-latest-reason">{upper(latest.retry_reason)}</span>
              </>
            )}
            {latest.replay_status && (
              <>
                <span className="text-gray-500">{t('runtime.refresh.replay_state', 'Replay state')}</span>
                <span data-testid="refresh-replay-state">{upper(latest.replay_status)}</span>
              </>
            )}
            {latest.cancel_reason && (
              <>
                <span className="text-gray-500">{t('runtime.refresh.cancel_reason', 'Cancellation reason')}</span>
                <span data-testid="refresh-cancel-reason">{latest.cancel_reason}</span>
              </>
            )}
            {latest.cancel_requested_by && (
              <>
                <span className="text-gray-500">{t('runtime.refresh.cancel_requested_by', 'Cancel requested by')}</span>
                <span data-testid="refresh-cancel-requested-by">{latest.cancel_requested_by}</span>
              </>
            )}
            {latest.cancel_requested_at && (
              <>
                <span className="text-gray-500">{t('runtime.refresh.cancel_requested_at', 'Cancel requested at')}</span>
                <span data-testid="refresh-cancel-requested-at">{latest.cancel_requested_at}</span>
              </>
            )}
          </div>

          <div className="mt-3 flex items-center gap-2">
            <input value={cancelReason} onChange={e => setCancelReason(e.target.value)}
              data-testid="refresh-cancel-reason-input" className="border rounded px-2 py-1 text-xs flex-1"
              placeholder={t('runtime.refresh.cancel_reason_placeholder', 'Cancellation reason')} />
            <button type="button" disabled={busy || !canCancel} onClick={() => void cancelRun()}
              data-testid="cancel-refresh-run" className="px-3 py-1.5 text-xs border rounded disabled:opacity-40">
              {t('runtime.refresh.cancel', 'Cancel run')}
            </button>
            {canReplay && (
              <button type="button" disabled={busy} onClick={() => void replayRunAction()}
                data-testid="replay-refresh-run" className="px-3 py-1.5 text-xs border rounded disabled:opacity-40">
                {t('runtime.refresh.replay', 'Replay via DLQ')}
              </button>
            )}
          </div>

          {cancelMessage && (
            <p className="mt-2 text-xs text-gray-600" data-testid="refresh-cancel-message">{cancelMessage}</p>
          )}
          {replayRun && (
            <p className="mt-2 text-xs text-gray-600">
              {t('runtime.refresh.replay_created', 'Replay run created')}: <span className="font-mono">{replayRun.run_id}</span>{' '}
              <span data-testid="refresh-replay-status" className="font-medium">{upper(replayRun.status)}</span>
            </p>
          )}
        </div>
      )}

      {error && <p className="mt-2 text-sm text-red-600" role="alert">{error}</p>}

      <button type="button" disabled={busy} onClick={() => void triggerRun()}
        data-testid="trigger-refresh-run" className="px-3 py-1.5 text-xs bg-black text-white rounded disabled:opacity-40 mb-4">
        {t('runtime.refresh.trigger', 'Trigger refresh')}
      </button>

      <form onSubmit={e => void updateSchedule(e)} className="border rounded-lg p-3 text-xs flex items-end gap-2">
        <label className="flex-1">
          {t('runtime.refresh.cron_expr', 'Cron expression')}
          <input value={cronExpr} onChange={e => setCronExpr(e.target.value)} placeholder="0 2 * * *"
            data-testid="refresh-schedule-cron" className="mt-1 w-full border rounded px-2 py-1" />
        </label>
        <label>
          {t('runtime.refresh.timezone', 'Timezone')}
          <input value={timezoneName} onChange={e => setTimezoneName(e.target.value)}
            data-testid="refresh-schedule-timezone" className="mt-1 w-full border rounded px-2 py-1" />
        </label>
        <button type="submit" disabled={busy || !cronExpr} data-testid="refresh-schedule-submit"
          className="px-3 py-1.5 bg-black text-white rounded disabled:opacity-40">
          {t('runtime.refresh.schedule_submit', 'Update schedule')}
        </button>
      </form>
    </div>
  )
}
