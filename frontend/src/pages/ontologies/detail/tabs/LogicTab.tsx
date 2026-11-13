import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useForm } from 'react-hook-form'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { ontologyApi } from '@/api/ontologies'
import { apiClient } from '@/api/client'
import ConfidenceBar from '@/components/ConfidenceBar'
import ConfirmDialog from '@/components/ConfirmDialog'
import { Pencil, Trash2, Plus, Search, ToggleLeft, ToggleRight } from 'lucide-react'
import type { LogicRule } from '@/types/ontology'

type LogicRuleRow = LogicRule & { name?: string }

const FUNCTION_TYPES = ['derived_property', 'aggregation', 'complex_edit', 'external_query'] as const

function parseLinkedEntities(value: unknown): string[] {
  if (Array.isArray(value)) return value
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value || '[]')
      return Array.isArray(parsed) ? parsed : []
    } catch {
      return []
    }
  }
  return []
}

export default function LogicTab({ ontologyId }: { ontologyId: string }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [showCreate, setShowCreate] = useState(false)
  const [searchQ, setSearchQ] = useState('')
  const [deleteTarget, setDeleteTarget] = useState<LogicRule | null>(null)
  const { register, handleSubmit, reset } = useForm<Partial<LogicRule>>()

  const { data: rules = [], isLoading } = useQuery({
    queryKey: ['logic', ontologyId],
    queryFn: () => ontologyApi.listLogic(ontologyId),
  })

  const createMut = useMutation({
    mutationFn: (data: Partial<LogicRule>) => ontologyApi.createLogic(ontologyId, data),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['logic', ontologyId] }); qc.invalidateQueries({ queryKey: ['stats'] }); setShowCreate(false); reset() },
  })

  const deleteMut = useMutation({
    mutationFn: (id: string) => ontologyApi.deleteLogic(ontologyId, id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['logic', ontologyId] }); qc.invalidateQueries({ queryKey: ['stats'] }) },
  })

  const toggleMut = useMutation({
    mutationFn: (id: string) => apiClient.post(`/ontologies/${ontologyId}/logic/${id}/toggle`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['logic', ontologyId] }),
  })

  const filtered = useMemo(() => {
    const q = searchQ.trim().toLowerCase()
    if (!q) return rules as LogicRule[]
    return (rules as LogicRule[]).filter(r =>
      r.name_cn?.toLowerCase().includes(q) || r.name_en?.toLowerCase().includes(q) ||
      r.description?.toLowerCase().includes(q) || r.definition?.toLowerCase().includes(q)
    )
  }, [rules, searchQ])

  return (
    <div className="space-y-4">
      {/* Search + Actions */}
      <div className="flex items-center gap-3">
        <div className="relative flex-1">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
          <input value={searchQ} onChange={e => setSearchQ(e.target.value)}
            placeholder={t('logic.ph_search')}
            className="w-full border rounded-lg pl-8 pr-3 py-2 text-sm" />
        </div>
        <button onClick={() => { setShowCreate(true); reset() }}
          className="flex items-center gap-2 px-3 py-2 bg-black text-white rounded-lg text-sm">
          <Plus size={14} /> {t('logic.add')}
        </button>
      </div>

      {/* Rules table */}
      <div className="bg-white border rounded-lg overflow-hidden">
        {isLoading ? <p className="py-8 text-center text-gray-400">{t('common.loading')}</p> : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm min-w-max">
              <thead className="bg-gray-50 border-b">
                <tr>
                  <th className="px-4 py-3 text-left text-gray-500 text-xs font-medium">{t('logic.col_name')}</th>
                  <th className="px-4 py-3 text-left text-gray-500 text-xs font-medium">{t('logic.col_type')}</th>
                  <th className="px-4 py-3 text-left text-gray-500 text-xs font-medium">{t('logic.col_definition')}</th>
                  <th className="px-4 py-3 text-left text-gray-500 text-xs font-medium">{t('logic.col_desc')}</th>
                  <th className="px-4 py-3 text-left text-gray-500 text-xs font-medium">{t('logic.col_linked_entities')}</th>
                  <th className="px-4 py-3 text-left text-gray-500 text-xs font-medium">{t('logic.col_status')}</th>
                  <th className="px-4 py-3 text-left text-gray-500 text-xs font-medium">{t('logic.col_confidence')}</th>
                  <th className="px-4 py-3 text-center text-gray-500 text-xs font-medium">{t('logic.col_enabled')}</th>
                  <th className="px-4 py-3 text-right text-gray-500 text-xs font-medium sticky right-0 bg-gray-50 z-10 border-l border-gray-200">{t('logic.col_actions')}</th>
                </tr>
              </thead>
              <tbody>
                {(filtered as LogicRuleRow[]).map(r => {
                  const linkedEntities = parseLinkedEntities(r.linked_entities)
                  const status = r.status || 'draft'
                  const enabled = r.enabled !== false
                  const definition = r.definition || ''
                  const description = r.description || ''
                  return (
                    <tr key={r.id} className="group border-b hover:bg-gray-50">
                      <td className="px-4 py-3 font-medium">{r.name_cn || r.name || '—'}</td>
                      <td className="px-4 py-3">
                        <span className="text-xs px-1.5 py-0.5 rounded border bg-gray-50 text-gray-600">
                          {r.function_type ? t(`logic.type_${r.function_type}`, r.function_type) : '—'}
                        </span>
                      </td>
                      <td className="px-4 py-3 font-mono text-xs text-gray-600 max-w-xs truncate" title={definition}>{definition || '—'}</td>
                      <td className="px-4 py-3 text-gray-500 max-w-xs truncate" title={description}>{description || '—'}</td>
                      <td className="px-4 py-3">
                        <div className="flex gap-1 flex-wrap">
                          {linkedEntities.map((e: string) => (
                            <span key={e} className="text-xs bg-blue-50 text-blue-600 px-1.5 py-0.5 rounded">{e}</span>
                          ))}
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        <span className={`text-xs px-1.5 py-0.5 rounded border ${
                          status === 'published' ? 'bg-green-50 text-green-700 border-green-200' :
                          status === 'draft' ? 'bg-amber-50 text-amber-700 border-amber-200' :
                          'bg-gray-50 text-gray-600'
                        }`}>{status}</span>
                      </td>
                      <td className="px-4 py-3 w-28"><ConfidenceBar value={r.confidence} /></td>
                      <td className="px-4 py-3 text-center">
                        <button onClick={() => toggleMut.mutate(r.id)} className="text-gray-500 hover:text-black">
                          {enabled ? <ToggleRight size={16} className="text-green-600" /> : <ToggleLeft size={16} className="text-gray-400" />}
                        </button>
                      </td>
                      <td className="px-4 py-3 text-right sticky right-0 bg-white group-hover:bg-gray-50 z-10 border-l border-gray-200">
                        <div className="inline-flex items-center gap-3">
                          <button onClick={() => navigate(`/ontologies/${ontologyId}/logic/${r.id}`)}
                            title={t('common.edit')} className="p-1.5 rounded text-blue-600 hover:bg-blue-50"><Pencil size={14} /></button>
                          <button onClick={() => setDeleteTarget(r)} title={t('common.delete')} className="p-1.5 rounded text-red-500 hover:bg-red-50"><Trash2 size={14} /></button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
        {!isLoading && filtered.length === 0 && <p className="text-center text-gray-400 py-8">{searchQ ? t('logic.no_match') : t('logic.empty')}</p>}
      </div>

      {showCreate && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-lg p-6 w-96">
            <h3 className="font-semibold mb-4">{t('logic.add')}</h3>
            <form onSubmit={handleSubmit(data => createMut.mutate(data))} className="space-y-3">
              <input {...register('name_cn', { required: true })} placeholder={t('entities.ph_name_cn')} className="w-full border rounded-lg px-3 py-2 text-sm" />
              <input {...register('name_en')} placeholder={t('entities.ph_name_en')} className="w-full border rounded-lg px-3 py-2 text-sm" />
              <select {...register('function_type')} defaultValue="derived_property" className="w-full border rounded-lg px-3 py-2 text-sm">
                {FUNCTION_TYPES.map(ft => <option key={ft} value={ft}>{t(`logic.type_${ft}`)}</option>)}
              </select>
              <textarea {...register('definition')} placeholder={t('logic.ph_definition')} rows={2} className="w-full border rounded-lg px-3 py-2 text-sm font-mono resize-none" />
              <textarea {...register('description')} placeholder={t('entities.ph_desc')} rows={2} className="w-full border rounded-lg px-3 py-2 text-sm resize-none" />
              <input {...register('confidence', { valueAsNumber: true })} type="number" step="0.01" min="0" max="1" placeholder={t('entities.ph_confidence')} className="w-full border rounded-lg px-3 py-2 text-sm" />
              <div className="flex justify-end gap-3 pt-2">
                <button type="button" onClick={() => { setShowCreate(false); reset() }} className="px-4 py-2 border rounded-lg text-sm">{t('common.cancel')}</button>
                <button type="submit" className="px-4 py-2 bg-black text-white rounded-lg text-sm">{t('common.save')}</button>
              </div>
            </form>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={!!deleteTarget}
        title={t('logic.delete_title')}
        message={t('logic.delete_confirm', { name: deleteTarget?.name_cn ?? '' })}
        onConfirm={() => { if (deleteTarget) deleteMut.mutate(deleteTarget.id); setDeleteTarget(null) }}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  )
}
