import { useState, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { useDropzone } from 'react-dropzone'
import { Upload, X, FileUp, Loader2, CheckCircle, XCircle } from 'lucide-react'
import { apiClientV2 } from '@/api/client'

const SOURCE_LABEL: Record<string, string> = { postgresql: 'PostgreSQL', mysql: 'MySQL', mongodb: 'MongoDB', rest_api: 'REST API' }
const DB_CONFIG_FIELDS: Record<string, { key: string; labelKey: string; label?: string; placeholder: string; type?: string }[]> = {
  postgresql: [{ key: 'host', labelKey: 'connectorInspector.field_host', placeholder: 'localhost' }, { key: 'port', labelKey: 'connectorInspector.field_port', placeholder: '5432' }, { key: 'database', labelKey: 'connectorInspector.field_database', placeholder: 'mydb' }, { key: 'user', labelKey: 'connectorInspector.field_user', placeholder: 'postgres' }, { key: 'password', labelKey: 'connectorInspector.field_password', placeholder: '••••••', type: 'password' }],
  mysql: [{ key: 'host', labelKey: 'connectorInspector.field_host', placeholder: 'localhost' }, { key: 'port', labelKey: 'connectorInspector.field_port', placeholder: '3306' }, { key: 'database', labelKey: 'connectorInspector.field_database', placeholder: 'mydb' }, { key: 'user', labelKey: 'connectorInspector.field_user', placeholder: 'root' }, { key: 'password', labelKey: 'connectorInspector.field_password', placeholder: '••••••', type: 'password' }],
  mongodb: [{ key: 'uri', labelKey: 'connectorInspector.field_uri', placeholder: 'mongodb://localhost:27017/mydb' }],
  rest_api: [{ key: 'url', labelKey: '', label: 'API URL', placeholder: 'https://api.example.com/data' }, { key: 'headers', labelKey: 'connectorInspector.field_headers', placeholder: '{"Authorization":"Bearer token"}' }, { key: 'method', labelKey: 'connectorInspector.field_method', placeholder: 'GET' }],
}

interface UploadedFileMeta {
  name: string
  size: number
  dataset_id?: string
  kind?: string
}

const NO_FILES: UploadedFileMeta[] = []

export default function ConnectorInspector({ config, onChange, readOnly = false }: { config: Record<string, unknown>; onChange: (key: string, value: unknown) => void; readOnly?: boolean }) {
  const { t } = useTranslation()
  const sourceType = String(config.source_type || 'file')
  const cv = (config.config_values || {}) as Record<string, string>
  const storedFiles = (config.files || NO_FILES) as UploadedFileMeta[]
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState('')
  const [testStatus, setTestStatus] = useState<'idle' | 'testing' | 'success' | 'failed'>('idle')
  const [testMessage, setTestMessage] = useState('')
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
  const hasDbConfig = sourceType !== 'file' && Object.keys(cv).length > 0

  const formatSize = (s: number | undefined) => s ? `(${(s / 1024).toFixed(1)} KB)` : ''

  if (readOnly) {
    return (
      <div className="space-y-3">
        <div className="bg-blue-50 border border-blue-100 rounded-lg p-3 text-xs">
          <p className="text-blue-700 font-medium mb-1">📋 {t('connectorInspector.saved_config_title')}</p>
          <p className="text-blue-600">{t('connectorInspector.type_label')}: {sourceType === 'file' ? t('connectorInspector.source_file') : (SOURCE_LABEL[sourceType] || sourceType)}</p>
          {sourceType === 'file' && hasStoredFiles && storedFiles.map((f, i: number) => (
            <p key={i} className="text-blue-500">📄 {f.name} {formatSize(f.size)}</p>
          ))}
          {sourceType !== 'file' && hasDbConfig && Object.entries(cv).filter(([k]) => k !== 'password').map(([k, v]) => (
            <p key={k} className="text-blue-500">{k}: {String(v).slice(0, 30)}</p>
          ))}
          {!hasStoredFiles && !hasDbConfig && <p className="text-blue-400">{t('connectorInspector.no_config')}</p>}
        </div>
      </div>
    )
  }

  return (
    <>
      <div><label className="text-xs text-gray-500 mb-1 block">{t('connectorInspector.source_type_label')}</label>
        <select value={sourceType} onChange={e => { onChange('source_type', e.target.value); onChange('config_values', {}); onChange('files', []); setTestStatus('idle') }} className="w-full border rounded-lg px-3 py-1.5 text-sm"><option value="file">{t('connectorInspector.source_file')}</option><option value="postgresql">PostgreSQL</option><option value="mysql">MySQL</option><option value="mongodb">MongoDB</option><option value="rest_api">REST API</option></select></div>
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
      {sourceType !== 'file' && (<div className="space-y-3">{DB_CONFIG_FIELDS[sourceType]?.map(f => (<div key={f.key}><label className="text-xs text-gray-500 mb-1 block">{f.label || t(f.labelKey)}</label><input type={f.type || 'text'} value={String((config.config_values as Record<string, string> | undefined)?.[f.key] || '')} onChange={e => { const cv2 = { ...((config.config_values as Record<string, string> | undefined) || {}), [f.key]: e.target.value }; onChange('config_values', cv2); setTestStatus('idle') }} placeholder={f.placeholder} className="w-full border rounded-lg px-3 py-1.5 text-sm" /></div>))}</div>)}
      <div>
        <button onClick={async () => { setTestStatus('testing'); try { if (sourceType === 'file') { setTestStatus(hasStoredFiles ? 'success' : 'failed'); setTestMessage(hasStoredFiles ? t('connectorInspector.test_ready') : t('connectorInspector.test_please_upload')); return } const result = await apiClientV2.post('/connections/test-config', { type: sourceType, config: cv }) as { success?: boolean, detail?: string }; if (result?.success) { setTestStatus('success'); setTestMessage(t('connectorInspector.test_success')) } else { setTestStatus('failed'); setTestMessage(result?.detail || t('connectorInspector.test_failed')) } } catch (e: unknown) { setTestStatus('failed'); setTestMessage((e as { detail?: string } | null | undefined)?.detail || t('connectorInspector.test_failed')) } }} disabled={testStatus === 'testing' || uploading}
          className={`w-full flex items-center justify-center gap-1.5 px-3 py-1.5 text-xs rounded-lg border ${testStatus === 'success' ? 'bg-green-50 text-green-700 border-green-200' : testStatus === 'failed' ? 'bg-red-50 text-red-700 border-red-200' : 'bg-white text-gray-600 border-gray-200 hover:bg-gray-50'}`}>
          {testStatus === 'testing' && <Loader2 size={11} className="animate-spin" />}{testStatus === 'success' ? <CheckCircle size={11} /> : testStatus === 'failed' ? <XCircle size={11} /> : null}{testStatus === 'testing' ? t('connectorInspector.test_testing') : testStatus === 'success' ? t('connectorInspector.test_success') : testStatus === 'failed' ? testMessage : t('connectorInspector.test_connection')}</button>
      </div>
      <div><label className="text-xs text-gray-500 mb-1 block">{t('connectorInspector.sync_mode_label')}</label><select value={String(config.sync_mode || 'snapshot')} onChange={e => onChange('sync_mode', e.target.value)} className="w-full border rounded-lg px-3 py-1.5 text-sm"><option value="snapshot">SNAPSHOT</option><option value="append">APPEND</option></select></div>
    </>
  )
}
