import { useState, useCallback, useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import { useDropzone } from 'react-dropzone'
import { Upload, X, FileUp, Loader2, CheckCircle, XCircle } from 'lucide-react'
import { apiClientV2 } from '@/api/client'

const SOURCE_LABEL: Record<string, string> = {
  postgresql: 'PostgreSQL', postgres: 'PostgreSQL', mysql: 'MySQL', mongo: 'MongoDB', mongodb: 'MongoDB', rest: 'REST API', rest_api: 'REST API',
}
const SQL_SOURCE_TYPES = new Set(['postgresql', 'postgres', 'mysql'])
const UNSUPPORTED_SOURCE_TYPES = new Set(['mongo', 'mongodb', 'rest', 'rest_api'])
const DB_CONFIG_FIELDS: Record<string, { key: string; labelKey: string; label?: string; placeholder: string; type?: string }[]> = {
  // Retained for existing unsaved builder definitions. New pipeline database
  // nodes use a saved connection selector below and never persist these fields.
  postgresql: [{ key: 'host', labelKey: 'connectorInspector.field_host', placeholder: 'localhost' }, { key: 'port', labelKey: 'connectorInspector.field_port', placeholder: '5432' }, { key: 'database', labelKey: 'connectorInspector.field_database', placeholder: 'mydb' }, { key: 'user', labelKey: 'connectorInspector.field_user', placeholder: 'postgres' }, { key: 'password', labelKey: 'connectorInspector.field_password', placeholder: '••••••', type: 'password' }],
  postgres: [{ key: 'host', labelKey: 'connectorInspector.field_host', placeholder: 'localhost' }, { key: 'port', labelKey: 'connectorInspector.field_port', placeholder: '5432' }, { key: 'database', labelKey: 'connectorInspector.field_database', placeholder: 'mydb' }, { key: 'user', labelKey: 'connectorInspector.field_user', placeholder: 'postgres' }, { key: 'password', labelKey: 'connectorInspector.field_password', placeholder: '••••••', type: 'password' }],
  mysql: [{ key: 'host', labelKey: 'connectorInspector.field_host', placeholder: 'localhost' }, { key: 'port', labelKey: 'connectorInspector.field_port', placeholder: '3306' }, { key: 'database', labelKey: 'connectorInspector.field_database', placeholder: 'mydb' }, { key: 'user', labelKey: 'connectorInspector.field_user', placeholder: 'root' }, { key: 'password', labelKey: 'connectorInspector.field_password', placeholder: '••••••', type: 'password' }],
  mongodb: [{ key: 'uri', labelKey: 'connectorInspector.field_uri', placeholder: 'mongodb://localhost:27017/mydb' }],
  rest_api: [{ key: 'url', labelKey: '', label: 'API URL', placeholder: 'https://api.example.com/data' }, { key: 'headers', labelKey: 'connectorInspector.field_headers', placeholder: '{"Authorization":"Bearer token"}' }, { key: 'method', labelKey: 'connectorInspector.field_method', placeholder: 'GET' }],
}

interface SavedConnection {
  id: string
  name: string
  kind: string
  status: string
}

interface UploadedFileMeta {
  name: string
  size: number
  dataset_id?: string
  kind?: string
}

const NO_FILES: UploadedFileMeta[] = []

function asList<T>(value: unknown, key: string): T[] {
  if (Array.isArray(value)) return value as T[]
  if (value && typeof value === 'object' && Array.isArray((value as Record<string, unknown>)[key])) {
    return (value as Record<string, unknown>)[key] as T[]
  }
  return []
}

export default function ConnectorInspector({ config, onChange, readOnly = false }: { config: Record<string, unknown>; onChange: (key: string, value: unknown) => void; readOnly?: boolean }) {
  const { t } = useTranslation()
  const sourceType = String(config.source_type || 'file')
  const isSqlSource = SQL_SOURCE_TYPES.has(sourceType)
  const isUnsupportedSource = UNSUPPORTED_SOURCE_TYPES.has(sourceType)
  const cv = (config.config_values || {}) as Record<string, string>
  const storedFiles = (config.files || NO_FILES) as UploadedFileMeta[]
  const connectionId = String(config.connection_id || '')
  const resource = String(config.resource || '')
  const [connections, setConnections] = useState<SavedConnection[]>([])
  const resourceKey = `${sourceType}:${connectionId}`
  const [resourceResult, setResourceResult] = useState<{ key: string; items: string[]; error: string } | null>(null)
  const currentResources = resourceResult?.key === resourceKey ? resourceResult : null
  const resources = currentResources?.items ?? []
  const resourceError = currentResources?.error ?? ''
  const resourceLoading = Boolean(isSqlSource && connectionId && !currentResources)
  const [connectionLoading, setConnectionLoading] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState('')
  const [testStatus, setTestStatus] = useState<'idle' | 'testing' | 'success' | 'failed'>('idle')
  const [testMessage, setTestMessage] = useState('')

  useEffect(() => {
    if (!isSqlSource) {
      return
    }
    let active = true
    void Promise.resolve()
      .then(() => {
        if (active) setConnectionLoading(true)
        return apiClientV2.get<SavedConnection[]>('/connections')
      })
      .then(value => { if (active) setConnections(asList<SavedConnection>(value, 'connections')) })
      .catch(() => { if (active) setConnections([]) })
      .finally(() => { if (active) setConnectionLoading(false) })
    return () => { active = false }
  }, [isSqlSource])

  useEffect(() => {
    if (!isSqlSource || !connectionId) {
      return
    }
    let active = true
    void apiClientV2.get<{ resources?: string[] }>(`/connections/${connectionId}/resources`)
      .then(value => { if (active) setResourceResult({ key: resourceKey, items: asList<string>(value, 'resources'), error: '' }) })
      .catch((e: unknown) => {
        if (!active) return
        const err = e as { detail?: string; message?: string } | null | undefined
        setResourceResult({ key: resourceKey, items: [], error: err?.detail || err?.message || t('connectorInspector.resources_failed') })
      })
    return () => { active = false }
  }, [connectionId, isSqlSource, resourceKey, t])

  const onDrop = useCallback(async (accepted: File[]) => {
    if (accepted.length === 0) return
    setUploading(true)
    setUploadError('')
    try {
      const uploaded: UploadedFileMeta[] = []
      for (const file of accepted) {
        const fd = new FormData()
        fd.append('file', file)
        const res = await apiClientV2.post<{ id?: string; kind?: string }>('/datasets/upload', fd, {
          headers: { 'Content-Type': 'multipart/form-data' },
        })
        uploaded.push({ name: file.name, size: file.size, dataset_id: res.id, kind: res.kind })
      }
      onChange('files', [...storedFiles, ...uploaded])
      setTestStatus('idle')
    } catch (e: unknown) {
      const err = e as { detail?: string; message?: string } | null | undefined
      setUploadError(err?.detail || err?.message || t('connectorInspector.upload_failed'))
    } finally {
      setUploading(false)
    }
  }, [storedFiles, onChange, t])
  const { getRootProps, getInputProps, isDragActive, open } = useDropzone({ onDrop, multiple: true, noClick: true })

  const hasStoredFiles = storedFiles.length > 0
  const hasDbConfig = !isUnsupportedSource && sourceType !== 'file' && (Boolean(connectionId && resource) || Object.keys(cv).length > 0)
  const formatSize = (s: number | undefined) => s ? `(${(s / 1024).toFixed(1)} KB)` : ''
  const selectedKind = sourceType === 'postgresql' ? 'postgres' : sourceType
  const matchingConnections = connections.filter(connection => connection.kind === selectedKind || (selectedKind === 'postgres' && connection.kind === 'postgresql'))

  if (isUnsupportedSource) {
    return (
      <div className="space-y-3">
        <div role="alert" className="bg-amber-50 border border-amber-200 rounded-lg p-3 text-xs">
          <p className="text-amber-800 font-medium mb-1">{SOURCE_LABEL[sourceType] || sourceType}</p>
          <p className="text-amber-700">{t('connectorInspector.unsupported_source', 'This source is unsupported in Builder. Use File Upload in Builder and manage external connections in Data Connections.')}</p>
        </div>
      </div>
    )
  }

  if (readOnly) {
    return (
      <div className="space-y-3">
        <div className="bg-blue-50 border border-blue-100 rounded-lg p-3 text-xs">
          <p className="text-blue-700 font-medium mb-1">📋 {t('connectorInspector.saved_config_title')}</p>
          <p className="text-blue-600">{t('connectorInspector.type_label')}: {sourceType === 'file' ? t('connectorInspector.source_file') : (SOURCE_LABEL[sourceType] || sourceType)}</p>
          {sourceType === 'file' && hasStoredFiles && storedFiles.map((f, i: number) => (
            <p key={i} className="text-blue-500">📄 {f.name} {formatSize(f.size)}</p>
          ))}
          {isSqlSource && connectionId && <p className="text-blue-500">{t('connectorInspector.saved_connection_label')}: {connectionId}</p>}
          {isSqlSource && resource && <p className="text-blue-500">{t('connectorInspector.source_table_label')}: {resource}</p>}
          {sourceType !== 'file' && !isSqlSource && hasDbConfig && Object.entries(cv).filter(([k]) => k !== 'password').map(([k, v]) => (
            <p key={k} className="text-blue-500">{k}: {String(v).slice(0, 30)}</p>
          ))}
          {!hasStoredFiles && !hasDbConfig && <p className="text-blue-400">{t('connectorInspector.no_config')}</p>}
        </div>
      </div>
    )
  }

  const handleSourceTypeChange = (value: string) => {
    onChange('source_type', value)
    onChange('connection_id', '')
    onChange('resource', '')
    onChange('config_values', {})
    onChange('files', [])
    setTestStatus('idle')
  }

  const handleConnectionChange = (value: string) => {
    onChange('connection_id', value)
    onChange('resource', '')
    // A saved connection is the only runtime credential source. This also
    // clears any legacy password entered in an older unsaved node.
    onChange('config_values', {})
    setTestStatus('idle')
  }

  const testConnection = async () => {
    if (sourceType === 'file') {
      setTestStatus(hasStoredFiles ? 'success' : 'failed')
      setTestMessage(hasStoredFiles ? t('connectorInspector.test_ready') : t('connectorInspector.test_please_upload'))
      return
    }
    if (isSqlSource && !connectionId) {
      setTestStatus('failed')
      setTestMessage(t('connectorInspector.select_connection'))
      return
    }
    setTestStatus('testing')
    try {
      const result = isSqlSource && connectionId
        ? await apiClientV2.post(`/connections/${connectionId}/test`, {}) as { success?: boolean; detail?: string }
        : await apiClientV2.post('/connections/test-config', { type: sourceType, config: cv }) as { success?: boolean; detail?: string }
      if (result?.success) {
        setTestStatus('success')
        setTestMessage(t('connectorInspector.test_success'))
      } else {
        setTestStatus('failed')
        setTestMessage(result?.detail || t('connectorInspector.test_failed'))
      }
    } catch (e: unknown) {
      setTestStatus('failed')
      setTestMessage((e as { detail?: string } | null | undefined)?.detail || t('connectorInspector.test_failed'))
    }
  }

  return (
    <>
      <div><label className="text-xs text-gray-500 mb-1 block" htmlFor="connector-source-type">{t('connectorInspector.source_type_label')}</label>
        <select id="connector-source-type" value={sourceType} onChange={e => handleSourceTypeChange(e.target.value)} className="w-full border rounded-lg px-3 py-1.5 text-sm"><option value="file">{t('connectorInspector.source_file')}</option><option value="postgresql">PostgreSQL</option><option value="mysql">MySQL</option></select></div>
      {sourceType === 'file' && (
        <div>
          <label className="text-xs text-gray-500 mb-1 block">{t('connectorInspector.upload_label')}</label>
          <div {...getRootProps()} onClick={open} className={`border-2 border-dashed rounded-lg p-4 text-center cursor-pointer transition-colors ${isDragActive ? 'border-blue-500 bg-blue-50' : 'border-gray-200 hover:border-gray-400'}`}>
            <input {...getInputProps()} /><Upload size={20} className="mx-auto mb-1 text-gray-400" />
            {uploading ? <p className="text-xs text-blue-500 font-medium">{t('connectorInspector.uploading')}</p> : isDragActive ? <p className="text-xs text-blue-500 font-medium">{t('connectorInspector.drop_release')}</p> : <p className="text-xs text-gray-500">{t('connectorInspector.drop_hint')}<span className="underline ml-0.5">{t('connectorInspector.drop_hint_click')}</span></p>}
            <p className="text-[10px] text-gray-400 mt-1">{t('connectorInspector.drop_formats_hint')}</p>
          </div>
          {uploadError && <p className="text-xs text-red-500 mt-1">{uploadError}</p>}
          {hasStoredFiles && (<div className="mt-2 space-y-1">{storedFiles.map((f, i: number) => (
            <div key={i} className="flex items-center gap-2 text-xs bg-gray-50 rounded px-2 py-1.5">
              <FileUp size={11} className="text-gray-400" /><span className="flex-1 truncate">{f.name}</span>
              <span className="text-gray-400">{formatSize(f.size)}</span>
              <button onClick={() => { onChange('files', storedFiles.filter((_, j: number) => j !== i)) }} className="text-gray-400 hover:text-red-500"><X size={11} /></button>
            </div>
          ))}</div>)}
        </div>
      )}
      {isSqlSource && (<div className="space-y-3">
        <div>
          <label className="text-xs text-gray-500 mb-1 block" htmlFor="connector-saved-connection">{t('connectorInspector.saved_connection_label')}</label>
          <select id="connector-saved-connection" aria-label={t('connectorInspector.saved_connection_label')} value={connectionId} onChange={e => handleConnectionChange(e.target.value)} disabled={connectionLoading} className="w-full border rounded-lg px-3 py-1.5 text-sm">
            <option value="">{connectionLoading ? t('connectorInspector.loading_connections') : t('connectorInspector.select_connection')}</option>
            {matchingConnections.map(connection => <option key={connection.id} value={connection.id}>{connection.name}</option>)}
          </select>
          {!connectionLoading && matchingConnections.length === 0 && <p className="text-[10px] text-gray-400 mt-1">{t('connectorInspector.no_saved_connections')}</p>}
        </div>
        <div>
          <label className="text-xs text-gray-500 mb-1 block" htmlFor="connector-source-table">{t('connectorInspector.source_table_label')}</label>
          <select id="connector-source-table" aria-label={t('connectorInspector.source_table_label')} value={resource} onChange={e => onChange('resource', e.target.value)} disabled={!connectionId || resourceLoading} className="w-full border rounded-lg px-3 py-1.5 text-sm">
            <option value="">{resourceLoading ? t('connectorInspector.loading_resources') : t('connectorInspector.select_table')}</option>
            {resources.map(table => <option key={table} value={table}>{table}</option>)}
          </select>
          {resourceError && <p className="text-xs text-red-500 mt-1">{resourceError}</p>}
        </div>
      </div>)}
      {sourceType !== 'file' && !isSqlSource && !isUnsupportedSource && (<div className="space-y-3">{DB_CONFIG_FIELDS[sourceType]?.map(f => (<div key={f.key}><label className="text-xs text-gray-500 mb-1 block">{f.label || t(f.labelKey)}</label><input type={f.type || 'text'} value={String((config.config_values as Record<string, string> | undefined)?.[f.key] || '')} onChange={e => { const cv2 = { ...((config.config_values as Record<string, string> | undefined) || {}), [f.key]: e.target.value }; onChange('config_values', cv2); setTestStatus('idle') }} placeholder={f.placeholder} className="w-full border rounded-lg px-3 py-1.5 text-sm" /></div>))}</div>)}
      <div>
        <button onClick={testConnection} disabled={testStatus === 'testing' || uploading}
          className={`w-full flex items-center justify-center gap-1.5 px-3 py-1.5 text-xs rounded-lg border ${testStatus === 'success' ? 'bg-green-50 text-green-700 border-green-200' : testStatus === 'failed' ? 'bg-red-50 text-red-700 border-red-200' : 'bg-white text-gray-600 border-gray-200 hover:bg-gray-50'}`}>
          {testStatus === 'testing' && <Loader2 size={11} className="animate-spin" />}{testStatus === 'success' ? <CheckCircle size={11} /> : testStatus === 'failed' ? <XCircle size={11} /> : null}{testStatus === 'testing' ? t('connectorInspector.test_testing') : testStatus === 'success' ? t('connectorInspector.test_success') : testStatus === 'failed' ? testMessage : t('connectorInspector.test_connection')}</button>
      </div>
      <div><label className="text-xs text-gray-500 mb-1 block" htmlFor="connector-sync-mode">{t('connectorInspector.sync_mode_label')}</label><select id="connector-sync-mode" value={String(config.sync_mode || 'snapshot')} onChange={e => onChange('sync_mode', e.target.value)} className="w-full border rounded-lg px-3 py-1.5 text-sm"><option value="snapshot">SNAPSHOT</option><option value="append">APPEND</option></select></div>
    </>
  )
}
