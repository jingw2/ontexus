import { useState, type Dispatch, type SetStateAction } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useForm, useWatch, type UseFormRegister, type UseFormHandleSubmit, type UseFormSetValue } from 'react-hook-form'
import { useTranslation } from 'react-i18next'
import { modelApi } from '@/api/ontologies'
import ConfirmDialog from '@/components/ConfirmDialog'
import type { ModelConfig } from '@/types/ontology'
import { Trash2, TestTube2, Plus, Pencil, X, Loader2 } from 'lucide-react'

const CONFIG_TYPES = [
  { value: 'llm', labelKey: 'model.config_type_llm' },
  { value: 'ocr', labelKey: 'model.config_type_ocr' },
  { value: 'other', labelKey: 'model.config_type_other' },
]

const PROVIDERS: Record<string, Array<{ value: string; label: string }>> = {
  llm: [
    { value: 'openai', label: 'OpenAI' },
    { value: 'anthropic', label: 'Anthropic' },
    { value: 'compatible', label: 'OpenAI-Compatible' },
  ],
  ocr: [
    { value: 'easyocr', label: 'EasyOCR' },
    { value: 'paddleocr', label: 'PaddleOCR' },
    { value: 'tesseract', label: 'Tesseract' },
    { value: 'external_api', label: 'External OCR API' },
  ],
  other: [
    { value: 'custom', label: 'Custom' },
    { value: 'local_service', label: 'Local Service' },
    { value: 'http_api', label: 'HTTP API' },
  ],
}

const USAGE_TAGS = ['VLM提取', '结构化提取', '宽表分析', 'Ontology Mapping', 'NL-to-Cypher', 'OCR文字提取']

interface ModelFormValues {
  name?: string
  config_type?: 'llm' | 'ocr' | 'other'
  provider?: string
  api_key?: string
  api_base?: string
  models_str?: string
  ocr_enabled?: string
  ocr_lang?: string
  ocr_device?: string
  options_json?: string
}

function modelList(text?: string) {
  return text ? text.split('\n').map((s: string) => s.trim()).filter(Boolean) : []
}

function parseOptions(text?: string) {
  if (!text?.trim()) return {}
  try {
    return JSON.parse(text)
  } catch {
    return {}
  }
}

function buildPayload(data: ModelFormValues, usageTags: string[]) {
  const options = {
    ...parseOptions(data.options_json),
    usage_tags: usageTags,
    ...(data.config_type === 'ocr' ? {
      enabled: data.ocr_enabled === 'true',
      lang: data.ocr_lang || 'ch',
      device: data.ocr_device || 'cpu',
    } : {}),
  }
  return {
    name: data.name,
    config_type: data.config_type || 'llm',
    provider: data.provider,
    api_key: data.api_key,
    api_base: data.api_base,
    models: modelList(data.models_str),
    options,
  }
}

function typeLabel(type: string | undefined, t: (key: string) => string) {
  const found = CONFIG_TYPES.find(ct => ct.value === (type || 'llm'))
  return found ? t(found.labelKey) : t('model.config_type_llm')
}

export default function ModelsPage() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [showCreate, setShowCreate] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<ModelConfig | null>(null)
  const [deleteError, setDeleteError] = useState('')
  const [testResult, setTestResult] = useState<Record<string, { msg: string; ok: boolean }>>({})
  const [formTags, setFormTags] = useState<string[]>([])
  const { register, handleSubmit, reset, setValue: setCreateValue, control } = useForm<ModelFormValues>({
    defaultValues: { config_type: 'llm', provider: 'openai', ocr_enabled: 'false', ocr_lang: 'ch', ocr_device: 'cpu' },
  })
  const createConfigType = useWatch({ control, name: 'config_type' }) || 'llm'

  const { data: models = [], isLoading } = useQuery({
    queryKey: ['models'], queryFn: () => modelApi.list(),
  })

  const [createError, setCreateError] = useState('')
  const createMut = useMutation({
    mutationFn: (data: ModelFormValues) => modelApi.create(buildPayload(data, formTags)),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['models'] }); setShowCreate(false); reset(); setFormTags([]); setCreateError('') },
    onError: (err: unknown) => {
      const err2 = err as { response?: { data?: { detail?: string } }; detail?: string; message?: string }
      setCreateError(err2?.response?.data?.detail || err2?.detail || err2?.message || t('model.save_failed'))
    },
  })

  const deleteMut = useMutation({
    mutationFn: (id: string) => modelApi.delete(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['models'] }); setDeleteTarget(null); setDeleteError('') },
    onError: (err: unknown) => {
      const detail = (err as { detail?: string })?.detail
      setDeleteError(detail === 'MODEL_REFERENCED' ? t('model.delete_failed_referenced') : t('model.delete_failed_generic'))
    },
  })

  const testMut = useMutation({
    mutationFn: (id: string) => modelApi.test(id),
    onSuccess: (res: unknown, id) => {
      const res2 = res as { data?: { ok?: boolean; response?: unknown }; ok?: boolean; response?: unknown }
      const data = res2?.data || res2
      setTestResult(prev => ({
        ...prev,
        [id]: data?.ok === false
          ? { msg: t('model.test_disabled_prefix', { detail: data.response || '' }), ok: false }
          : { msg: t('model.test_ok'), ok: true },
      }))
    },
    onError: (err: unknown, id) => {
      const err2 = err as { detail?: string }
      setTestResult(prev => ({ ...prev, [id]: { msg: `❌ ${err2?.detail || t('model.connection_failed')}`, ok: false } }))
    },
  })

  // ── 编辑 ──
  const [editTarget, setEditTarget] = useState<ModelConfig | null>(null)
  const [editTags, setEditTags] = useState<string[]>([])
  const { register: regEdit, handleSubmit: handleEditSubmit, setValue, control: editControl } = useForm<ModelFormValues>()
  const editConfigType = useWatch({ control: editControl, name: 'config_type' }) || 'llm'

  const updateMut = useMutation({
    mutationFn: ({ id, data }: { id: string; data: ModelFormValues }) => modelApi.update(id, buildPayload(data, editTags)),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['models'] }); setEditTarget(null); setEditTags([]) },
  })

  const openEdit = (m: ModelConfig) => {
    const options = m.options || {}
    setEditTarget(m); setEditTags((options.usage_tags as string[]) || [])
    setValue('name', m.name); setValue('config_type', m.config_type || 'llm'); setValue('provider', m.provider)
    setValue('api_base', m.api_base || '')
    setValue('models_str', (m.models || []).join('\n'))
    setValue('ocr_enabled', options.enabled ? 'true' : 'false')
    setValue('ocr_lang', String(options.lang || 'ch'))
    setValue('ocr_device', String(options.device || 'cpu'))
    setValue('options_json', JSON.stringify(
      Object.fromEntries(Object.entries(options).filter(([k]) => !['usage_tags', 'lang', 'device'].includes(k))),
      null,
      2,
    ))
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-semibold">{t('model.title')}</h2>
        <button onClick={() => { setShowCreate(true); reset({ config_type: 'llm', provider: 'openai', ocr_enabled: 'false', ocr_lang: 'ch', ocr_device: 'cpu' }); setFormTags([]) }}
          className="flex items-center gap-2 bg-black text-white px-4 py-2 rounded-lg text-sm">
          <Plus size={14} /> {t('model.create')}
        </button>
      </div>

      <div className="grid gap-4">
        {isLoading ? <p className="text-gray-400 text-sm">{t('common.loading')}</p> :
          (models as ModelConfig[]).map(m => (
            <div key={m.id} className="bg-white border rounded-lg p-4">
              <div className="flex items-start justify-between">
                <div>
                  <h3 className="font-semibold">{m.name}</h3>
                  <p className="text-sm text-gray-500">{typeLabel(m.config_type, t)} · {m.provider}{m.api_base ? ` · ${m.api_base}` : ''}</p>
                  {m.models?.length > 0 && (
                    <div className="flex gap-1 mt-2 flex-wrap">
                      {m.models.map(mn => <span key={mn} className="bg-gray-100 text-gray-700 text-xs px-2 py-0.5 rounded">{mn}</span>)}
                    </div>
                  )}
                  {((m.options?.usage_tags as string[]) || []).length > 0 && (
                    <div className="flex gap-1 flex-wrap mt-1">
                      {((m.options?.usage_tags as string[]) || []).map((tag: string) => (
                        <span key={tag} className="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded-full">{tag}</span>
                      ))}
                    </div>
                  )}
                  {testResult[m.id] && <p className={`text-xs mt-1 ${testResult[m.id].ok ? 'text-green-600' : 'text-amber-600'}`}>{testResult[m.id].msg}</p>}
                </div>
                <div className="flex gap-2 shrink-0">
                  <button onClick={() => testMut.mutate(m.id)} disabled={testMut.isPending} className="inline-flex items-center gap-1 px-2.5 py-1.5 border rounded text-xs hover:bg-gray-50 disabled:opacity-50"><TestTube2 size={13} />{t('model.test_short')}</button>
                  <button onClick={() => openEdit(m)} className="inline-flex items-center gap-1 px-2.5 py-1.5 border rounded text-xs hover:bg-gray-50 text-blue-600"><Pencil size={13} />{t('common.edit')}</button>
                  <button onClick={() => { setDeleteError(''); setDeleteTarget(m) }} className="inline-flex items-center gap-1 px-2.5 py-1.5 border rounded text-xs hover:bg-gray-50 text-red-500"><Trash2 size={13} />{t('common.delete')}</button>
                </div>
              </div>
            </div>
          ))
        }
        {!isLoading && (models as ModelConfig[]).length === 0 && (
          <div className="bg-white border rounded-lg p-8 text-center text-gray-400">{t('model.empty')}</div>
        )}
      </div>

      {/* 新建弹窗 */}
      {showCreate && <ModelFormModal title={t('model.create')} onClose={() => { setShowCreate(false); setCreateError('') }} onSubmit={(d: ModelFormValues) => createMut.mutate(d)}
        isPending={createMut.isPending} formTags={formTags} setFormTags={setFormTags} register={register}
        handleSubmit={handleSubmit} configType={createConfigType} setValue={setCreateValue} error={createError} />}

      {/* 编辑弹窗 */}
      {editTarget && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={() => setEditTarget(null)}>
          <div className="bg-white rounded-lg shadow-lg p-6 w-[480px] max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
            <div className="flex justify-between items-center mb-4">
              <h3 className="font-semibold">{t('model.edit_title')}</h3>
              <button onClick={() => setEditTarget(null)} className="text-gray-400 hover:text-black"><X size={16} /></button>
            </div>
            <form onSubmit={handleEditSubmit(d => updateMut.mutate({ id: editTarget.id, data: { ...d, api_key: d.api_key || undefined } }))} className="space-y-3">
              <div><label className="block text-sm font-medium mb-1">{t('model.name')} *</label>
                <input {...regEdit('name', { required: true })} className="w-full border rounded-lg px-3 py-2 text-sm" /></div>
              <div><label className="block text-sm font-medium mb-1">{t('model.config_type_label')} *</label>
                <select {...regEdit('config_type', { required: true, onChange: e => setValue('provider', PROVIDERS[e.target.value]?.[0]?.value || 'custom') })} className="w-full border rounded-lg px-3 py-2 text-sm">
                  {CONFIG_TYPES.map(ct => <option key={ct.value} value={ct.value}>{t(ct.labelKey)}</option>)}
                </select></div>
              <div><label className="block text-sm font-medium mb-1">Provider *</label>
                <select {...regEdit('provider', { required: true })} className="w-full border rounded-lg px-3 py-2 text-sm">
                  {(PROVIDERS[editConfigType] || PROVIDERS.llm).map(p => <option key={p.value} value={p.value}>{p.label}</option>)}
                </select></div>
              <div><label className="block text-sm font-medium mb-1">{t('model.api_key_hint')}</label>
                <input {...regEdit('api_key')} type="password" autoComplete="new-password" className="w-full border rounded-lg px-3 py-2 text-sm" /></div>
              <div><label className="block text-sm font-medium mb-1">{t('model.api_base')}</label>
                <input {...regEdit('api_base')} className="w-full border rounded-lg px-3 py-2 text-sm" /></div>
              <div><label className="block text-sm font-medium mb-1">{t('model.models_label')}</label>
                <textarea {...regEdit('models_str')} rows={3} className="w-full border rounded-lg px-3 py-2 text-sm font-mono" /></div>
              {editConfigType === 'ocr' && (
                <div className="grid grid-cols-3 gap-3">
                  <div><label className="block text-sm font-medium mb-1">{t('model.ocr_enabled_label')}</label>
                    <select {...regEdit('ocr_enabled')} className="w-full border rounded-lg px-3 py-2 text-sm">
                      <option value="false">{t('model.opt_disabled')}</option><option value="true">{t('model.opt_enabled')}</option>
                    </select></div>
                  <div><label className="block text-sm font-medium mb-1">{t('model.ocr_lang_label')}</label>
                    <input {...regEdit('ocr_lang')} placeholder="ch" className="w-full border rounded-lg px-3 py-2 text-sm" /></div>
                  <div><label className="block text-sm font-medium mb-1">{t('model.device_label')}</label>
                    <select {...regEdit('ocr_device')} className="w-full border rounded-lg px-3 py-2 text-sm">
                      <option value="cpu">CPU</option><option value="gpu">GPU</option>
                    </select></div>
                </div>
              )}
              <div><label className="block text-sm font-medium mb-1">{t('model.advanced_json_label')}</label>
                <textarea {...regEdit('options_json')} rows={3} placeholder={'{"timeout": 30}'} className="w-full border rounded-lg px-3 py-2 text-sm font-mono" /></div>
              <div><label className="text-xs text-gray-500 mb-2 block">{t('model.usage_tags_label')}</label>
                <div className="flex flex-wrap gap-2">
                  {USAGE_TAGS.map(tag => {
                    const sel = editTags.includes(tag)
                    return <button key={tag} type="button" onClick={() => setEditTags(prev => sel ? prev.filter(tg => tg !== tag) : [...prev, tag])}
                      className={`text-xs px-3 py-1.5 rounded-full border ${sel ? 'bg-black text-white border-black' : 'border-gray-200 text-gray-600'}`}>{tag}</button>
                  })}
                </div></div>
              <div className="flex justify-end gap-3 pt-2">
                <button type="button" onClick={() => setEditTarget(null)} className="px-4 py-2 border rounded-lg text-sm">{t('common.cancel')}</button>
                <button type="submit" disabled={updateMut.isPending} className="flex items-center gap-1.5 px-4 py-2 bg-black text-white rounded-lg text-sm disabled:opacity-50">
                  {updateMut.isPending && <Loader2 size={13} className="animate-spin" />}{t('common.save')}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      <ConfirmDialog open={!!deleteTarget} title={t('model.confirm_delete')} message={t('model.confirm_delete_msg', { name: deleteTarget?.name })}
        error={deleteError}
        onConfirm={() => deleteTarget && deleteMut.mutate(deleteTarget.id)}
        onCancel={() => { setDeleteTarget(null); setDeleteError('') }} />
    </div>
  )
}

/** 新建模型表单弹窗 */
function ModelFormModal({
  title, onClose, onSubmit, isPending, formTags, setFormTags, register, handleSubmit, configType, setValue, error,
}: {
  title: string
  onClose: () => void
  onSubmit: (values: ModelFormValues) => void
  isPending: boolean
  formTags: string[]
  setFormTags: Dispatch<SetStateAction<string[]>>
  register: UseFormRegister<ModelFormValues>
  handleSubmit: UseFormHandleSubmit<ModelFormValues>
  configType: string
  setValue: UseFormSetValue<ModelFormValues>
  error: string
}) {
  const { t } = useTranslation()
  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-white rounded-lg shadow-lg p-6 w-[480px] max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
        <h3 className="font-semibold mb-4">{title}</h3>
        {error && <div className="mb-3 p-2.5 bg-red-50 border border-red-200 rounded-lg text-red-600 text-sm">{error}</div>}
        <form onSubmit={handleSubmit(onSubmit)} className="space-y-3">
          <div><label className="block text-sm font-medium mb-1">{t('model.name')} *</label>
            <input {...register('name', { required: true })} className="w-full border rounded-lg px-3 py-2 text-sm" /></div>
          <div><label className="block text-sm font-medium mb-1">{t('model.config_type_label')} *</label>
            <select {...register('config_type', { required: true, onChange: e => setValue('provider', PROVIDERS[e.target.value]?.[0]?.value || 'custom') })} className="w-full border rounded-lg px-3 py-2 text-sm">
              {CONFIG_TYPES.map(ct => <option key={ct.value} value={ct.value}>{t(ct.labelKey)}</option>)}
            </select></div>
          <div><label className="block text-sm font-medium mb-1">Provider *</label>
            <select {...register('provider', { required: true })} className="w-full border rounded-lg px-3 py-2 text-sm">
              {(PROVIDERS[configType] || PROVIDERS.llm).map(p => <option key={p.value} value={p.value}>{p.label}</option>)}
            </select></div>
          <div><label className="block text-sm font-medium mb-1">{t('model.api_key')}</label>
            <input {...register('api_key')} type="password" className="w-full border rounded-lg px-3 py-2 text-sm" /></div>
          <div><label className="block text-sm font-medium mb-1">{t('model.api_base')}</label>
            <input {...register('api_base')} placeholder="https://api.openai.com/v1" className="w-full border rounded-lg px-3 py-2 text-sm" /></div>
          <div><label className="block text-sm font-medium mb-1">{t('model.models_label')}</label>
            <textarea {...register('models_str')} rows={3} placeholder="gpt-4o&#10;gpt-4o-mini" className="w-full border rounded-lg px-3 py-2 text-sm font-mono" /></div>
          {configType === 'ocr' && (
            <div className="grid grid-cols-3 gap-3">
              <div><label className="block text-sm font-medium mb-1">{t('model.ocr_enabled_label')}</label>
                <select {...register('ocr_enabled')} className="w-full border rounded-lg px-3 py-2 text-sm">
                  <option value="false">{t('model.opt_disabled')}</option><option value="true">{t('model.opt_enabled')}</option>
                </select></div>
              <div><label className="block text-sm font-medium mb-1">{t('model.ocr_lang_label')}</label>
                <input {...register('ocr_lang')} placeholder="ch" className="w-full border rounded-lg px-3 py-2 text-sm" /></div>
              <div><label className="block text-sm font-medium mb-1">{t('model.device_label')}</label>
                <select {...register('ocr_device')} className="w-full border rounded-lg px-3 py-2 text-sm">
                  <option value="cpu">CPU</option><option value="gpu">GPU</option>
                </select></div>
            </div>
          )}
          <div><label className="block text-sm font-medium mb-1">{t('model.advanced_json_label')}</label>
            <textarea {...register('options_json')} rows={3} placeholder={'{"timeout": 30}'} className="w-full border rounded-lg px-3 py-2 text-sm font-mono" /></div>
          <div><label className="text-xs text-gray-500 mb-2 block">{t('model.usage_tags_label')}</label>
            <div className="flex flex-wrap gap-2">{[...USAGE_TAGS].map(tag => {
              const sel = formTags.includes(tag)
              return <button key={tag} type="button" onClick={() => setFormTags((prev: string[]) => sel ? prev.filter((tg: string) => tg !== tag) : [...prev, tag])}
                className={`text-xs px-3 py-1.5 rounded-full border ${sel ? 'bg-black text-white border-black' : 'border-gray-200 text-gray-600 hover:bg-gray-50'}`}>{tag}</button>
            })}</div></div>
          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose} className="px-4 py-2 border rounded-lg text-sm">{t('common.cancel')}</button>
            <button type="submit" disabled={isPending} className="flex items-center gap-1.5 px-4 py-2 bg-black text-white rounded-lg text-sm disabled:opacity-50">
              {isPending && <Loader2 size={13} className="animate-spin" />}{t('common.save')}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
