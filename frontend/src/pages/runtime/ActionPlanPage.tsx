import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useParams, Link } from 'react-router-dom'
import { runtimeApi } from '@/api/runtime'
import type { ActionPlan, ApprovalReceipt, ExecutionNotStarted, ExecutionReceipt } from '@/types/runtime'

function isStarted(receipt: ExecutionReceipt | ExecutionNotStarted | null): receipt is ExecutionReceipt {
  return receipt !== null && receipt.status !== 'not_started'
}

/**
 * Task 27: exact-plan approval/execution operator surface
 * (`GET/POST /api/v2/runtime/action-plans/{planId}[/approve|/execute]`).
 * The operator reviews the server-computed plan (diff, impact, risk,
 * exact hash) and can only approve/execute the plan's OWN stored hash —
 * this page never lets an operator edit parameters, a selector, or the
 * hash itself; every write is the backend's own governed decision.
 */
export default function ActionPlanPage() {
  const { planId } = useParams<{ planId: string }>()
  return <ActionPlanContent key={planId} planId={planId} />
}

function ActionPlanContent({ planId }: { planId?: string }) {
  const { t } = useTranslation()
  const [plan, setPlan] = useState<ActionPlan | null>(null)
  const [receipt, setReceipt] = useState<ApprovalReceipt | null>(null)
  const [execution, setExecution] = useState<ExecutionReceipt | ExecutionNotStarted | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    if (!planId) return
    runtimeApi.getActionPlan(planId)
      .then(value => { setPlan(value); setError('') })
      .catch(() => setError(t('runtime.plan.load_failed', 'Failed to load the action plan')))
    runtimeApi.getExecutionStatus(planId).then(setExecution).catch(() => {})
  }, [planId, t])

  useEffect(() => { void load() }, [load])

  const approve = async () => {
    if (!planId || !plan) return
    setBusy(true)
    setError('')
    try {
      const r = await runtimeApi.approveActionPlan(planId, plan.plan_hash)
      setReceipt(r)
    } catch {
      setError(t('runtime.plan.approve_failed', 'Approval was denied or the plan hash no longer matches'))
    } finally {
      setBusy(false)
    }
  }

  const execute = async () => {
    if (!planId || !plan) return
    setBusy(true)
    setError('')
    try {
      const r = await runtimeApi.executeActionPlan(planId, plan.plan_hash)
      setExecution(r)
    } catch {
      setError(t('runtime.plan.execute_failed', 'Execution was denied or the plan hash no longer matches'))
    } finally {
      setBusy(false)
    }
  }

  if (error && !plan) {
    return <p className="text-sm text-red-600" role="alert" data-testid="action-plan-error">{error}</p>
  }
  if (!plan) {
    return <div className="p-6 text-gray-400" data-testid="action-plan-loading">{t('common.loading', 'Loading…')}</div>
  }

  const requiresApproval = Boolean((plan.policy_decision as { requires_hitl?: boolean })?.requires_hitl)

  return (
    <div data-testid="action-plan-page">
      <h2 className="text-base font-medium mb-3">{t('runtime.plan.title', 'Action Plan')}</h2>

      <dl className="grid grid-cols-2 gap-1 text-sm mb-4">
        <dt className="text-gray-500">{t('runtime.plan.id', 'Plan ID')}</dt>
        <dd className="font-mono">{plan.id}</dd>
        <dt className="text-gray-500">{t('runtime.plan.hash', 'Plan hash')}</dt>
        <dd className="font-mono break-all" data-testid="plan-hash">{plan.plan_hash}</dd>
        <dt className="text-gray-500">{t('runtime.plan.action', 'Action')}</dt>
        <dd className="font-mono">{plan.action_id}</dd>
        <dt className="text-gray-500">{t('runtime.plan.risk', 'Risk classification')}</dt>
        <dd data-testid="plan-risk-classification">{plan.risk_classification}</dd>
        <dt className="text-gray-500">{t('runtime.plan.snapshot', 'Snapshot')}</dt>
        <dd className="font-mono">{plan.semantic_snapshot_id}</dd>
        <dt className="text-gray-500">{t('runtime.plan.expiry', 'Expiry')}</dt>
        <dd>{plan.expiry}</dd>
      </dl>

      <div className="grid grid-cols-2 gap-3 mb-4 text-xs">
        <div className="border rounded-lg p-2">
          <p className="text-gray-500 mb-1">{t('runtime.plan.predicted_diff', 'Predicted before/after diff')}</p>
          <pre className="whitespace-pre-wrap break-all" data-testid="plan-predicted-diff">{JSON.stringify(plan.predicted_diff, null, 2)}</pre>
        </div>
        <div className="border rounded-lg p-2">
          <p className="text-gray-500 mb-1">{t('runtime.plan.impact_scope', 'Impact scope')}</p>
          <pre className="whitespace-pre-wrap break-all" data-testid="plan-impact-scope">{JSON.stringify(plan.impact_scope, null, 2)}</pre>
        </div>
      </div>

      {plan.evidence_citations.length > 0 && (
        <ul className="list-disc pl-4 text-xs text-gray-600 mb-3" data-testid="plan-evidence-citations">
          {plan.evidence_citations.map((c, i) => <li key={i}>{c.source_type}:{c.source_id} · {c.locator}</li>)}
        </ul>
      )}
      {plan.rule_outcomes.length > 0 && (
        <ul className="list-disc pl-4 text-xs text-gray-600 mb-3" data-testid="plan-rule-outcomes">
          {plan.rule_outcomes.map((r, i) => <li key={i}>{r.rule_id}: {r.result}</li>)}
        </ul>
      )}

      <Link to={`/runtime/sandbox/${plan.id}`} className="text-xs underline text-gray-500" data-testid="view-sandbox-link">
        {t('runtime.plan.view_sandbox', 'View sandbox simulation')}
      </Link>

      {error && <p className="mt-3 text-sm text-red-600" role="alert">{error}</p>}

      <div className="mt-4 flex gap-2">
        <button type="button" disabled={busy} onClick={() => void approve()}
          data-testid="approve-exact-plan" className="px-3 py-1.5 text-xs bg-black text-white rounded disabled:opacity-40">
          {requiresApproval ? t('runtime.plan.approve', 'Approve exact plan') : t('runtime.plan.approve_optional', 'Record approval (optional)')}
        </button>
        <button type="button" disabled={busy} onClick={() => void execute()}
          data-testid="execute-exact-plan" className="px-3 py-1.5 text-xs border rounded disabled:opacity-40">
          {t('runtime.plan.execute', 'Execute exact plan')}
        </button>
      </div>

      {receipt && (
        <div className="mt-3 text-xs text-gray-600" data-testid="approval-receipt">
          {t('runtime.plan.approved_by', 'Approved by')} {receipt.approver_user_id} {t('runtime.plan.at', 'at')} {receipt.approved_at}
        </div>
      )}

      {execution && (
        <div className="mt-3 text-sm" data-testid="execution-receipt">
          <span className="text-gray-500 mr-2">{t('runtime.plan.execution_status', 'Execution status')}</span>
          <span data-testid="execution-status" className="font-medium">{execution.status.toUpperCase()}</span>
          {isStarted(execution) && execution.reconciliation_case_id && (
            <Link to={`/runtime/reconciliation/${execution.reconciliation_case_id}`}
              className="ml-3 text-xs underline text-gray-500" data-testid="view-reconciliation-link">
              {t('runtime.plan.view_reconciliation', 'View reconciliation case')}
            </Link>
          )}
        </div>
      )}
    </div>
  )
}
