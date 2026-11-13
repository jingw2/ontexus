import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useParams } from 'react-router-dom'
import { runtimeApi } from '@/api/runtime'
import type { ActionPlan, ReconciliationCase } from '@/types/runtime'

/**
 * Task 27: reconciliation query + rollback-plan creation surface
 * (`GET /api/v2/runtime/reconciliations/{id}`,
 * `POST /api/v2/runtime/executions/{executionId}/rollback-plans`).
 * Admin-only (see `AdminRoute` in `App.tsx`) — matches the existing
 * "和解操作" reconciliation surface's own admin gate. A freshly created
 * rollback `ActionPlan` carries no execution of its own yet: it is always
 * a reviewable proposal, never an already-applied write, so its state is
 * always rendered as `PROPOSAL_ONLY` — this is a UI-owned description of
 * that invariant, not a field the server returns.
 */
export default function ReconciliationPage() {
  const { reconciliationId } = useParams<{ reconciliationId: string }>()
  return <ReconciliationContent key={reconciliationId} reconciliationId={reconciliationId} />
}

function ReconciliationContent({ reconciliationId }: { reconciliationId?: string }) {
  const { t } = useTranslation()
  const [reconciliation, setReconciliation] = useState<ReconciliationCase | null>(null)
  const [rollbackPlan, setRollbackPlan] = useState<ActionPlan | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    if (!reconciliationId) return
    runtimeApi.getReconciliation(reconciliationId)
      .then(value => { setReconciliation(value); setError('') })
      .catch(() => setError(t('runtime.reconciliation.load_failed', 'Failed to load the reconciliation case')))
  }, [reconciliationId, t])

  useEffect(() => { void load() }, [load])

  const createRollback = async () => {
    if (!reconciliation) return
    setBusy(true)
    setError('')
    try {
      const plan = await runtimeApi.createRollbackPlan(reconciliation.execution_id)
      setRollbackPlan(plan)
    } catch {
      setError(t('runtime.reconciliation.rollback_failed', 'Rollback plan creation was denied'))
    } finally {
      setBusy(false)
    }
  }

  if (error && !reconciliation) {
    return <p className="text-sm text-red-600" role="alert" data-testid="reconciliation-page-error">{error}</p>
  }
  if (!reconciliation) {
    return <div className="p-6 text-gray-400" data-testid="reconciliation-page-loading">{t('common.loading', 'Loading…')}</div>
  }

  return (
    <div data-testid="runtime-reconciliation-page">
      <h2 className="text-base font-medium mb-3">{t('runtime.reconciliation.title', 'Reconciliation Case')}</h2>
      <dl className="grid grid-cols-2 gap-1 text-sm mb-4">
        <dt className="text-gray-500">{t('runtime.reconciliation.id', 'Case ID')}</dt>
        <dd className="font-mono">{reconciliation.id}</dd>
        <dt className="text-gray-500">{t('runtime.reconciliation.status', 'Status')}</dt>
        <dd data-testid="reconciliation-status" className="font-medium">{reconciliation.status}</dd>
        <dt className="text-gray-500">{t('runtime.reconciliation.execution', 'Execution')}</dt>
        <dd className="font-mono">{reconciliation.execution_id}</dd>
        <dt className="text-gray-500">{t('runtime.reconciliation.plan', 'Plan')}</dt>
        <dd className="font-mono">{reconciliation.plan_id}</dd>
        {reconciliation.unknown_reason && (
          <>
            <dt className="text-gray-500">{t('runtime.reconciliation.unknown_reason', 'Unknown reason')}</dt>
            <dd data-testid="reconciliation-unknown-reason">{reconciliation.unknown_reason}</dd>
          </>
        )}
        {reconciliation.next_action && (
          <>
            <dt className="text-gray-500">{t('runtime.reconciliation.next_action', 'Next action')}</dt>
            <dd data-testid="reconciliation-next-action">{reconciliation.next_action}</dd>
          </>
        )}
      </dl>

      <div className="border rounded-lg p-2 text-xs mb-4">
        <p className="text-gray-500 mb-1">{t('runtime.reconciliation.observed_effect', 'Observed effect')}</p>
        <pre className="whitespace-pre-wrap break-all" data-testid="reconciliation-observed-effect">
          {JSON.stringify(reconciliation.observed_effect, null, 2)}
        </pre>
      </div>

      {error && <p className="mt-3 text-sm text-red-600" role="alert">{error}</p>}

      <p className="text-xs text-gray-500 mb-1" data-testid="rollback-limitation-notice">
        {t(
          'runtime.reconciliation.rollback_limitation',
          'Rollback plans currently only succeed for a not-yet-reachable unbound-plan scenario — every plan executed through the governed bound-action path today will be denied.',
        )}
      </p>
      <button type="button" disabled={busy} onClick={() => void createRollback()}
        data-testid="create-rollback-plan" className="px-3 py-1.5 text-xs bg-black text-white rounded disabled:opacity-40">
        {t('runtime.reconciliation.create_rollback', 'Create rollback plan')}
      </button>

      {rollbackPlan && (
        <div className="mt-3 text-sm" data-testid="rollback-plan-result">
          <div>
            <span className="text-gray-500 mr-2">{t('runtime.reconciliation.rollback_plan_hash', 'Rollback plan hash')}</span>
            <span data-testid="rollback-plan-hash" className="font-mono break-all">{rollbackPlan.plan_hash}</span>
          </div>
          <div>
            <span className="text-gray-500 mr-2">{t('runtime.reconciliation.rollback_state', 'State')}</span>
            <span data-testid="rollback-execution-state" className="font-medium">PROPOSAL_ONLY</span>
          </div>
        </div>
      )}
    </div>
  )
}
