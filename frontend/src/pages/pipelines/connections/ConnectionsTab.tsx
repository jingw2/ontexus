import { useState, useEffect, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { useDropzone } from 'react-dropzone'
import { Plus, Database, FileUp, Globe, X, Loader2, RefreshCw, CheckCircle, XCircle } from 'lucide-react'
import { apiClientV2 } from '@/api/client'
import { useAuthStore } from '@/stores/authStore'

interface Connection {
  id: string
  name: string
  kind: string
  status: string
}

const KIND_META: Record<string, { icon: React.ReactNode; labelKey: string }> = {
  file:     { icon: <FileUp size={14} />,   labelKey: 'connectorInspector.source_file' },
  mysql:    { icon: <Database size={14} />, labelKey: '' },
  postgres: { icon: <Database size={14} />, labelKey: '' },
  mongo:    { icon: <Database size={14} />, labelKey: '' },
  rest:     { icon: <Globe size={14} />,    labelKey: '' },
}
const KIND_LABEL_FALLBACK: Record<string, string> = { mysql: 'MySQL', postgres: 'PostgreSQL', mongo: 'MongoDB', rest: 'REST API' }

const STATUS_STYLE: Record<string, string> = {
  active:   'text-green-600 bg-green-50 border-green-200',
  inactive: 'text-gray-400 bg-gray-50 border-gray-200',
  error:    'text-red-500 bg-red-50 border-red-200',
}

const STATUS_LABEL_KEY: Record<string, string> = {
  active: 'connectionsTab.status_active', inactive: 'connectionsTab.status_inactive', error: 'connectionsTab.status_error',
}

const KIND_CONFIG_FIELDS: Record<string, { key: string; labelKey: string; placeholder: string; type?: string }[]> = {
  mysql:    [
    { key: 'host', labelKey: 'connectorInspector.field_host', placeholder: 'localhost' },
    { key: 'port', labelKey: 'connectorInspector.field_port', placeholder: '3306' },
    { key: 'database', labelKey: 'connectorInspector.field_database', placeholder: 'mydb' },
    { key: 'user', labelKey: 'connectorInspector.field_user', placeholder: 'root' },
    { key: 'password', labelKey: 'connectorInspector.field_password', placeholder: '••••••', type: 'password' },
  ],
  postgres: [
    { key: 'host', labelKey: 'connectorInspector.field_host', placeholder: 'localhost' },
    { key: 'port', labelKey: 'connectorInspector.field_port', placeholder: '5432' },
    { key: 'database', labelKey: 'connectorInspector.field_database', placeholder: 'mydb' },
    { key: 'user', labelKey: 'connectorInspector.field_user', placeholder: 'postgres' },
    { key: 'password', labelKey: 'connectorInspector.field_password', placeholder: '••••••', type: 'password' },
  ],
  mongo:    [
    { key: 'uri', labelKey: 'connectorInspector.field_uri', placeholder: 'mongodb://localhost:27017/mydb' },
  ],
  rest:     [
    { key: 'url', labelKey: '', placeholder: 'https://api.example.com/data' },
    { key: 'headers', labelKey: 'connectorInspector.field_headers', placeholder: '{"Authorization": "Bearer token"}' },
  ],
  file: [],
}

function FileUploadZone({ files, onFilesChange }: { files: File[]; onFilesChange: (f: File[]) => void }) {
  const { t } = useTranslation()
  const onDrop = useCallback((accepted: File[]) => {
    onFilesChange([...files, ...accepted])
  }, [files, onFilesChange])

  const { getRootProps, getInputProps, isDragActive, open } = useDropzone({ onDrop, multiple: true, noClick: true })

  return (
    <div>
      <div
        {...getRootProps()}
        onClick={open}
        className={`border-2 border-dashed rounded-xl p-8 text-center cursor-pointer transition-colors
          ${isDragActive ? 'border-black bg-gray-50' : 'border-gray-200 hover:border-gray-400'}`}
      >
        <input {...getInputProps()} />
        <FileUp size={28} className="mx-auto mb-2 text-gray-400" />
        {isDragActive ? (
          <p className="text-sm text-black font-medium">{t('connectionsTab.drop_release')}</p>
        ) : (
          <>
            <p className="text-sm text-gray-600">{t('connectionsTab.drop_hint')}<span className="underline ml-1 cursor-pointer">{t('connectionsTab.drop_hint_click')}</span></p>
            <p className="text-xs text-gray-400 mt-1">{t('connectionsTab.drop_formats_hint')}</p>
          </>
        )}
      </div>
      {files.length > 0 && (
        <div className="mt-3 space-y-1.5">
          {files.map((f, i) => (
            <div key={i} className="flex items-center gap-2 text-xs bg-gray-50 rounded-lg px-3 py-2">
              <FileUp size={12} className="text-gray-400 shrink-0" />
              <span className="flex-1 truncate text-gray-700">{f.name}</span>
              <span className="text-gray-400">{(f.size / 1024).toFixed(1)} KB</span>
              <button
                type="button"
                onClick={() => onFilesChange(files.filter((_, j) => j !== i))}
                className="text-gray-400 hover:text-red-500"
              >
                <X size={12} />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function ConnectionsTab() {
  const { t } = useTranslation()
  const role = useAuthStore(s => s.user?.role)
  const canEdit = role === 'editor' || role === 'admin'
  const [connections, setConnections] = useState<Connection[]>([])
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')
  const [syncing, setSyncing] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)
  const [testStatus, setTestStatus] = useState<'idle' | 'success' | 'failed'>('idle')
  const [testMessage, setTestMessage] = useState('')
  const [expandedConnection, setExpandedConnection] = useState<string | null>(null)
  const [resources, setResources] = useState<Record<string, string[]>>({})
  const [resourceErrors, setResourceErrors] = useState<Record<string, string>>({})
  const [resourceLoading, setResourceLoading] = useState<string | null>(null)

  const [formName, setFormName] = useState('')
  const [formKind, setFormKind] = useState('mysql')
  const [formConfig, setFormConfig] = useState<Record<string, string>>({})
  const [formSyncMode, setFormSyncMode] = useState<'snapshot' | 'append'>('snapshot')
  const [formFiles, setFormFiles] = useState<File[]>([])

  const fetchConnections = () => {
    apiClientV2.get('/connections')
      .then((res: unknown) => setConnections(Array.isArray(res) ? res : ((res as { data?: Connection[] })?.data ?? [])))
      .catch(() => setConnections([]))
      .finally(() => setLoading(false))
  }

  const loadConnections = () => {
    setLoading(true)
    fetchConnections()
  }

  useEffect(() => { fetchConnections() }, [])

  const resetForm = () => {
    setFormName('')
    setFormKind('mysql')
    setFormConfig({})
    setFormSyncMode('snapshot')
    setFormFiles([])
    setFormError('')
    setTestStatus('idle')
    setTestMessage('')
  }

  const handleTest = async () => {
    if (!canEdit) return
    if (formKind === 'file') {
      setTestStatus(formFiles.length > 0 ? 'success' : 'failed')
      setTestMessage(formFiles.length > 0 ? t('connectionsTab.test_success') : t('connectionsTab.files_required_error'))
      return
    }
    setTesting(true)
    setTestStatus('idle')
    setTestMessage('')
    try {
      const result = await apiClientV2.post('/connections/test-config', { type: formKind, config: formConfig }) as { success?: boolean; detail?: string }
      if (result?.success) {
        setTestStatus('success')
        setTestMessage(t('connectionsTab.test_success'))
      } else {
        setTestStatus('failed')
        setTestMessage(result?.detail || t('connectionsTab.test_failed'))
      }
    } catch (e: unknown) {
      const err = e as { detail?: string; message?: string } | null | undefined
      setTestStatus('failed')
      setTestMessage(err?.detail || err?.message || t('connectionsTab.test_failed'))
    } finally {
      setTesting(false)
    }
  }

  const handleSave = async () => {
    if (!canEdit) return
    if (!formName.trim()) { setFormError(t('connectionsTab.name_required_error')); return }
    if (formKind === 'file' && formFiles.length === 0) { setFormError(t('connectionsTab.files_required_error')); return }
    setSaving(true)
    setFormError('')
    try {
      if (formKind === 'file') {
        for (const file of formFiles) {
          const fd = new FormData()
          fd.append('file', file)
          await apiClientV2.post('/datasets/upload', fd, {
            headers: { 'Content-Type': 'multipart/form-data' },
          })
        }
        await apiClientV2.post('/connections', {
          name: formName, kind: 'file',
          config: { files: formFiles.map(f => f.name), sync_mode: formSyncMode },
        })
      } else {
        await apiClientV2.post('/connections', {
          name: formName, kind: formKind,
          config: { ...formConfig, sync_mode: formSyncMode },
        })
      }
      setShowForm(false)
      resetForm()
      loadConnections()
    } catch (e: unknown) {
      const err = e as { detail?: string; response?: { data?: { detail?: string } }; message?: string }
      setFormError(err?.detail || err?.response?.data?.detail || err?.message || t('connectionsTab.save_failed'))
    } finally {
      setSaving(false)
    }
  }

  const handleSync = async (id: string) => {
    if (!canEdit) return
    setSyncing(id)
    try {
      await apiClientV2.post(`/connections/${id}/sync`, {})
      loadConnections()
    } catch {
      // ignore sync errors silently
    } finally {
      setSyncing(null)
    }
  }

  const handleDelete = async (id: string) => {
    if (!canEdit) return
    if (!window.confirm(t('connectionsTab.confirm_delete'))) return
    await apiClientV2.delete(`/connections/${id}`)
    loadConnections()
  }

  const discoverResources = async (id: string) => {
    if (!canEdit) return
    if (expandedConnection === id && resources[id]) {
      setExpandedConnection(null)
      return
    }
    setResourceLoading(id)
    setResourceErrors(previous => ({ ...previous, [id]: '' }))
    try {
      const result = await apiClientV2.get<{ resources?: string[] }>(`/connections/${id}/resources`)
      const list = Array.isArray(result) ? result as unknown as string[] : (result?.resources || [])
      setResources(previous => ({ ...previous, [id]: list }))
      setExpandedConnection(id)
    } catch (e: unknown) {
      const err = e as { detail?: string; message?: string } | null | undefined
      setResourceErrors(previous => ({
        ...previous,
        [id]: err?.detail || err?.message || t('connectionsTab.resources_failed'),
      }))
      setExpandedConnection(id)
    } finally {
      setResourceLoading(null)
    }
  }

  if (loading) return <div className="text-gray-400 text-sm p-4">{t('common.loading')}</div>

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h2 className="text-lg font-semibold">{t('connectionsTab.title')}</h2>
          <p className="text-xs text-gray-400 mt-0.5">{t('connectionsTab.subtitle')}</p>
        </div>
        {canEdit && <button
          onClick={() => { resetForm(); setShowForm(true) }}
          className="flex items-center gap-2 bg-black text-white px-4 py-2 rounded-lg text-sm"
        >
          <Plus size={14} /> {t('connectionsTab.new_connection')}
        </button>}
      </div>

      {canEdit && showForm && (
        <div className="border rounded-xl p-5 bg-white space-y-4">
          <div className="flex justify-between items-center">
            <h3 className="font-medium text-sm">{t('connectionsTab.new_connection')}</h3>
            <button onClick={() => { setShowForm(false); resetForm() }} className="text-gray-400 hover:text-black">
              <X size={16} />
            </button>
          </div>

          <div>
            <label className="text-xs text-gray-500 mb-1 block" htmlFor="connection-name">{t('connectionsTab.name_label')} *</label>
            <input
              id="connection-name"
              value={formName}
              onChange={e => setFormName(e.target.value)}
              placeholder={t('connectionsTab.name_ph')}
              className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-black"
            />
          </div>

          <div>
            <label className="text-xs text-gray-500 mb-2 block">{t('connectionsTab.type_label')}</label>
            <div className="flex gap-2 flex-wrap">
              {Object.entries(KIND_META).map(([k, m]) => (
                <button
                  key={k}
                  type="button"
                  onClick={() => { setFormKind(k); setFormConfig({}); setFormFiles([]) }}
                  className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs border transition-colors
                    ${formKind === k ? 'bg-black text-white border-black' : 'border-gray-200 text-gray-600 hover:bg-gray-50'}`}
                >
                  {m.icon} {m.labelKey ? t(m.labelKey) : KIND_LABEL_FALLBACK[k]}
                </button>
              ))}
            </div>
          </div>

          {formKind === 'file' ? (
            <FileUploadZone files={formFiles} onFilesChange={setFormFiles} />
          ) : (
            <div className="space-y-3">
              {KIND_CONFIG_FIELDS[formKind]?.map(f => (
                <div key={f.key}>
                  <label className="text-xs text-gray-500 mb-1 block">{f.labelKey ? t(f.labelKey) : 'API URL'}</label>
                  <input
                    type={f.type || 'text'}
                    value={formConfig[f.key] || ''}
                    onChange={e => setFormConfig(p => ({ ...p, [f.key]: e.target.value }))}
                    placeholder={f.placeholder}
                    className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-black"
                  />
                </div>
              ))}
            </div>
          )}

          <div>
            <label className="text-xs text-gray-500 mb-2 block">{t('connectionsTab.sync_mode_label')}</label>
            <div className="flex gap-4">
              {(['snapshot', 'append'] as const).map(m => (
                <label key={m} className="flex items-center gap-2 text-sm cursor-pointer">
                  <input
                    type="radio"
                    name="sync_mode"
                    value={m}
                    checked={formSyncMode === m}
                    onChange={() => setFormSyncMode(m)}
                    className="accent-black"
                  />
                  <span>{m === 'snapshot' ? t('connectionsTab.sync_snapshot_label') : t('connectionsTab.sync_append_label')}</span>
                </label>
              ))}
            </div>
          </div>

          {formError && <p className="text-red-500 text-xs">{formError}</p>}
          {testStatus !== 'idle' && <p className={`text-xs flex items-center gap-1 ${testStatus === 'success' ? 'text-green-600' : 'text-red-500'}`}>
            {testStatus === 'success' ? <CheckCircle size={12} /> : <XCircle size={12} />} {testMessage}
          </p>}

          <div className="flex gap-2 justify-end">
            <button onClick={() => { setShowForm(false); resetForm() }} className="px-4 py-2 text-sm border rounded-lg hover:bg-gray-50">
              {t('common.cancel')}
            </button>
            <button
              onClick={handleTest}
              disabled={testing || saving}
              className="flex items-center gap-2 px-4 py-2 text-sm border rounded-lg hover:bg-gray-50 disabled:opacity-50"
            >
              {testing && <Loader2 size={13} className="animate-spin" />}
              {testing ? t('connectionsTab.testing') : t('connectionsTab.test_connection')}
            </button>
            <button
              onClick={handleSave}
              disabled={saving}
              className="flex items-center gap-2 px-4 py-2 text-sm bg-black text-white rounded-lg disabled:opacity-50"
            >
              {saving && <Loader2 size={13} className="animate-spin" />}
              {saving ? t('connectionsTab.saving') : t('common.save')}
            </button>
          </div>
        </div>
      )}

      {connections.length === 0 ? (
        <div className="border-2 border-dashed rounded-xl p-10 text-center text-gray-400 space-y-2">
          <Database size={28} className="mx-auto opacity-30" />
          <p className="text-sm">{t('connectionsTab.empty')}</p>
          <p className="text-xs">{t('connectionsTab.empty_hint')}</p>
        </div>
      ) : (
        <div className="border rounded-xl divide-y overflow-hidden">
          {connections.map(c => {
            const meta = KIND_META[c.kind] ?? KIND_META.file
            const statusStyle = STATUS_STYLE[c.status] ?? STATUS_STYLE.inactive
            const statusLabel = STATUS_LABEL_KEY[c.status] ? t(STATUS_LABEL_KEY[c.status]) : c.status
            const isSqlConnection = ['mysql', 'postgres', 'postgresql'].includes(c.kind)
            return (
              <div key={c.id}>
                <div className="p-4 flex items-center gap-3">
                  <div className="w-8 h-8 bg-gray-100 rounded-lg flex items-center justify-center text-gray-500">
                    {meta.icon}
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="font-medium text-sm truncate">{c.name}</p>
                    <p className="text-xs text-gray-400">{meta.labelKey ? t(meta.labelKey) : KIND_LABEL_FALLBACK[c.kind]}</p>
                  </div>
                  <span className={`text-xs font-medium px-2 py-0.5 rounded border ${statusStyle}`}>
                    {statusLabel}
                  </span>
                  {canEdit && isSqlConnection && (
                    <button
                      onClick={() => discoverResources(c.id)}
                      disabled={resourceLoading === c.id}
                      className="flex items-center gap-1 text-xs px-2.5 py-1.5 border rounded-lg hover:bg-gray-50 disabled:opacity-50 transition-colors"
                    >
                      {resourceLoading === c.id && <Loader2 size={11} className="animate-spin" />}
                      {expandedConnection === c.id ? t('connectionsTab.hide_tables') : t('connectionsTab.discover_tables')}
                    </button>
                  )}
                  {canEdit && (isSqlConnection ? (
                    <span className="text-[10px] text-gray-400 max-w-36 text-right">
                      {t('connectionsTab.pipeline_sync_hint')}
                    </span>
                  ) : (
                    <button
                      onClick={() => handleSync(c.id)}
                      disabled={syncing === c.id}
                      className="flex items-center gap-1 text-xs px-2.5 py-1.5 border rounded-lg hover:bg-gray-50 disabled:opacity-50 transition-colors"
                    >
                      <RefreshCw size={11} className={syncing === c.id ? 'animate-spin' : ''} />
                      {t('connectionsTab.sync_button')}
                    </button>
                  ))}
                  {canEdit && <button
                    onClick={() => handleDelete(c.id)}
                    className="text-gray-400 hover:text-red-500 text-xs px-1 transition-colors"
                  >
                    {t('common.delete')}
                  </button>}
                </div>
                {canEdit && expandedConnection === c.id && (
                  <div className="px-16 pb-4 pt-0">
                    {resourceErrors[c.id] ? (
                      <p className="text-xs text-red-500">{resourceErrors[c.id]}</p>
                    ) : resources[c.id]?.length ? (
                      <div className="flex flex-wrap gap-1.5">
                        {resources[c.id].map(resource => <span key={resource} className="text-xs bg-gray-50 border rounded px-2 py-1 text-gray-600">{resource}</span>)}
                      </div>
                    ) : <p className="text-xs text-gray-400">{t('connectionsTab.no_tables')}</p>}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
