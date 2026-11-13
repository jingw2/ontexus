import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useForm } from 'react-hook-form'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { ontologyApi } from '@/api/ontologies'
import { apiClient } from '@/api/client'
import ConfirmDialog from '@/components/ConfirmDialog'
import { Pencil, Trash2, Plus, ToggleLeft, ToggleRight } from 'lucide-react'
import type { Action } from '@/types/ontology'

type ActionRow = Action

export default function ActionsTab({ ontologyId }: { ontologyId: string }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [showCreate, setShowCreate] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<Action | null>(null)
  const { register, handleSubmit, reset } = useForm<Partial<Action>>()

  const { data: actions = [], isLoading } = useQuery({
    queryKey: ['actions', ontologyId],
    queryFn: () => ontologyApi.listActions(ontologyId),
  })

  const createMut = useMutation({
    mutationFn: (data: Partial<Action>) => ontologyApi.createAction(ontologyId, data),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['actions', ontologyId] }); qc.invalidateQueries({ queryKey: ['stats'] }); setShowCreate(false); reset() },
  })
  const deleteMut = useMutation({
    mutationFn: (id: string) => ontologyApi.deleteAction(ontologyId, id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['actions', ontologyId] }); qc.invalidateQueries({ queryKey: ['stats'] }) },
  })
  const toggleMut = useMutation({
    mutationFn: (id: string) => apiClient.post(`/ontologies/${ontologyId}/actions/${id}/toggle`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['actions', ontologyId] }),
  })

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <div className="flex-1" />
        <button onClick={() => { setShowCreate(true); reset() }}
          className="flex items-center gap-2 px-3 py-2 bg-black text-white rounded-lg text-sm">
          <Plus size={14} /> {t('actions.add')}
        </button>
      </div>
      <div className="bg-white border rounded-lg overflow-hidden">
        {isLoading ? <p className="py-8 text-center text-gray-400">{t('common.loading')}</p> : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm min-w-max">
              <thead className="bg-gray-50 border-b">
                <tr>
                  <th className="px-4 py-3 text-left text-gray-500 text-xs font-medium">{t('actions.col_name')}</th>
                  <th className="px-4 py-3 text-left text-gray-500 text-xs font-medium">{t('actions.col_rule')}</th>
                  <th className="px-4 py-3 text-left text-gray-500 text-xs font-medium">{t('actions.col_desc')}</th>
                  <th className="px-4 py-3 text-left text-gray-500 text-xs font-medium">{t('actions.col_linked_entities')}</th>
                  <th className="px-4 py-3 text-left text-gray-500 text-xs font-medium">{t('actions.col_status')}</th>
                  <th className="px-4 py-3 text-center text-gray-500 text-xs font-medium">{t('actions.col_enabled')}</th>
                    <th className="px-4 py-3 text-right text-gray-500 text-xs font-medium sticky right-0 bg-gray-50 z-10 border-l border-gray-200">{t('actions.col_actions')}</th>
                </tr>
              </thead>
              <tbody>
              {(actions as ActionRow[]).map(a => {
                const le = (typeof a.linked_entities === 'string' ? JSON.parse(a.linked_entities || '[]') : a.linked_entities) || []
                const rules = a.rules || []
                const rulesSummary = rules.length ? `${rules[0].operation} ${rules[0].target}${rules.length > 1 ? ` +${rules.length - 1}` : ''}` : '—'
                const status = a.status || 'draft'
                const enabled = a.enabled !== false
                const description = a.description || ''
                return (
                  <tr key={a.id} className="group border-b hover:bg-gray-50">
                    <td className="px-4 py-3 font-medium">{a.name_cn}</td>
                    <td className="px-4 py-3">
                      <span className="text-xs px-1.5 py-0.5 rounded border bg-gray-50 text-gray-600 font-mono" title={rulesSummary}>{rulesSummary}</span>
                    </td>
                    <td className="px-4 py-3 text-gray-500 max-w-xs truncate" title={description}>{description || '—'}</td>
                      <td className="px-4 py-3">
                        <div className="flex gap-1 flex-wrap">
                          {le.map((e: string) => (
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
                      <td className="px-4 py-3 text-center">
                        <button onClick={() => toggleMut.mutate(a.id)} className="text-gray-500 hover:text-black">
                          {enabled ? <ToggleRight size={16} className="text-green-600" /> : <ToggleLeft size={16} className="text-gray-400" />}
                        </button>
                      </td>
                      <td className="px-4 py-3 text-right sticky right-0 bg-white group-hover:bg-gray-50 z-10 border-l border-gray-200" onClick={ev => ev.stopPropagation()}>
                        <div className="inline-flex items-center gap-3">
                          <button onClick={() => navigate(`/ontologies/${ontologyId}/actions/${a.id}`)} title={t('common.edit')} className="p-1.5 rounded text-blue-500 hover:bg-blue-50"><Pencil size={14} /></button>
                          <button onClick={() => setDeleteTarget(a)} title={t('common.delete')} className="p-1.5 rounded text-red-500 hover:bg-red-50"><Trash2 size={14} /></button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
        {!isLoading && (actions as Action[]).length === 0 && <p className="text-center text-gray-400 py-8">{t('actions.empty')}</p>}
      </div>
      {showCreate && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-lg p-6 w-96">
            <h3 className="font-semibold mb-4">{t('actions.add')}</h3>
            <form onSubmit={handleSubmit(data => createMut.mutate(data))} className="space-y-3">
              <input {...register('name_cn', { required: true })} placeholder={t('entities.ph_name_cn')} className="w-full border rounded-lg px-3 py-2 text-sm" />
              <input {...register('name_en')} placeholder={t('entities.ph_name_en')} className="w-full border rounded-lg px-3 py-2 text-sm" />
              <textarea {...register('description')} placeholder={t('entities.ph_desc')} rows={2} className="w-full border rounded-lg px-3 py-2 text-sm resize-none" />
              <p className="text-xs text-gray-400">{t('actions.create_hint')}</p>
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
        title={t('actions.delete_title')}
        message={t('actions.delete_confirm', { name: deleteTarget?.name_cn ?? '' })}
        onConfirm={() => { if (deleteTarget) deleteMut.mutate(deleteTarget.id); setDeleteTarget(null) }}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  )
}
