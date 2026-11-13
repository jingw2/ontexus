import React, { useState, useEffect, lazy, Suspense } from 'react'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import { useIsMutating, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ontologyApi } from '@/api/ontologies'
import StatusBadge from '@/components/StatusBadge'
import InfoTab from './tabs/InfoTab'
import FilesTab from './tabs/FilesTab'
import EntitiesTab from './tabs/EntitiesTab'
import LogicTab from './tabs/LogicTab'
import ActionsTab from './tabs/ActionsTab'
import AuditTab from './tabs/AuditTab'
import CuratedDatasetsTab from './tabs/CuratedDatasetsTab'
import OntologyPublicationPanel from './OntologyPublicationPanel'


const GraphTab = lazy(() => import('./tabs/GraphTabV2'))

type Tab = 'info' | 'graph' | 'entities' | 'logic' | 'actions' | 'files' | 'extract' |  'audit' | 'curated'

class GraphErrorBoundary extends React.Component<
  { children: React.ReactNode; fallbackLabel?: string; retryLabel?: string },
  { hasError: boolean; error: string }
> {
  constructor(props: { children: React.ReactNode; fallbackLabel?: string; retryLabel?: string }) {
    super(props)
    this.state = { hasError: false, error: '' }
  }
  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error: error.message }
  }
  render() {
    if (this.state.hasError) {
      return (
        <div className="bg-red-50 border border-red-200 rounded-lg p-8 text-center">
          <p className="text-red-600 font-medium mb-2">{this.props.fallbackLabel || 'Chart failed to load'}</p>
          <p className="text-red-400 text-sm font-mono">{this.state.error}</p>
          <button
            onClick={() => this.setState({ hasError: false, error: '' })}
            className="mt-4 px-3 py-1.5 text-sm border border-red-300 text-red-500 rounded-lg hover:bg-red-100">
            {this.props.retryLabel || 'Retry'}
          </button>
        </div>
      )
    }
    return this.props.children
  }
}

export default function OntologyDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const { t } = useTranslation()
  const [searchParams] = useSearchParams()
  const initialTab = (searchParams.get('tab') as Tab) || 'info'
  const [activeTab, setActiveTab] = useState<Tab>(initialTab)

  const { data: ontology, isLoading } = useQuery({
    queryKey: ['ontology', id],
    queryFn: () => ontologyApi.get(id!),
    enabled: !!id,
  })
  const qc = useQueryClient()
  const [localStatus, setLocalStatus] = useState<string | null>(null)

  // tab edits mark the working copy dirty server-side — refetch the ontology
  // once they settle so the "publish new version" button can appear
  const mutating = useIsMutating()
  useEffect(() => {
    if (mutating === 0) qc.invalidateQueries({ queryKey: ['ontology', id] })
  }, [mutating, qc, id])

  if (isLoading) return <div className="p-6 text-gray-400">{t('common.loading')}</div>
  if (!ontology) return <div className="p-6 text-red-500">Ontology not found</div>

  // lifecycle transitions update the header badge immediately and invalidate
  // the list/overview cache so returning to the list shows the new status
  const status = localStatus ?? ontology.status
  const handleStatusChange = (next: string) => {
    setLocalStatus(next)
    qc.setQueryData(['ontology', id], (old: unknown) => {
      if (old && typeof old === 'object') return { ...(old as Record<string, unknown>), status: next }
      return old
    })
  }
  const invalidateList = () => {
    qc.invalidateQueries({ queryKey: ['ontologies'] })
    qc.invalidateQueries({ queryKey: ['stats'] })
    qc.invalidateQueries({ queryKey: ['ontology', id] })
  }

  const isPipelineMode = ontology.build_mode === 'pipeline_mapping'

  const tabs: { key: Tab; label: string }[] = [
    { key: 'info', label: t('ontology.tabs.info') },
    { key: 'graph', label: t('ontology.tabs.graph') },
    { key: 'entities', label: t('ontology.tabs.entities') },
    { key: 'logic', label: t('ontology.tabs.logic') },
    { key: 'actions', label: t('ontology.tabs.actions') },
    { key: 'audit', label: t('ontology.tabs.audit') },
    isPipelineMode
      ? { key: 'curated', label: t('ontology.tabs.curated') }
      : { key: 'files', label: t('ontology.tabs.files') },
  ]

  return (
    <div>
      <div className="flex items-center gap-4 mb-6">
        <button onClick={() => navigate('/ontologies')} className="text-gray-500 hover:text-black text-sm">{t('ontology.back')}</button>
        <h2 className="text-xl font-semibold">{ontology.name}</h2>
        <StatusBadge status={status} />
        <span className="text-gray-400 text-sm">{ontology.domain} · {ontology.version}</span>
      </div>

      <div className="mb-6">
        <OntologyPublicationPanel ontologyId={id!} status={status} isDirty={!!ontology.is_dirty}
          onStatusChange={handleStatusChange} onMutated={invalidateList} />
      </div>

      <div className="border-b mb-6">
        <div className="flex gap-1">
          {tabs.map(tab => (
            <button key={tab.key}
              onClick={() => setActiveTab(tab.key)}
              className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                activeTab === tab.key
                  ? 'border-black text-black'
                  : 'border-transparent text-gray-500 hover:text-gray-700'
              }`}>
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      <div>
        {activeTab === 'info' && <InfoTab ontology={ontology} />}
        {activeTab === 'files' && <FilesTab ontologyId={id!} />}
        {activeTab === 'curated' && <CuratedDatasetsTab ontologyId={id!} />}
        {activeTab === 'graph' && (
          <GraphErrorBoundary fallbackLabel={t('ontology.graph_render_failed')} retryLabel={t('common.retry')}>
            <Suspense fallback={<div className="text-gray-400 py-8 text-center">{t('common.loading')}</div>}>
              <GraphTab ontologyId={id!} />
            </Suspense>
          </GraphErrorBoundary>
        )}
        {activeTab === 'entities' && <EntitiesTab ontologyId={id!} />}
        {activeTab === 'logic' && <LogicTab ontologyId={id!} />}
        {activeTab === 'actions' && <ActionsTab ontologyId={id!} />}
        {activeTab === 'audit' && <AuditTab ontologyId={id!} />}
      </div>
    </div>
  )
}
