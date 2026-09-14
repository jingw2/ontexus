import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useForm, type UseFormRegister } from 'react-hook-form'
import { useTranslation } from 'react-i18next'
import type { TFunction } from 'i18next'
import {
  toolConnectionsApi, PROVIDER_KINDS, LIVE_PROVIDER_KINDS, SEARCH_PROVIDER_PRESETS,
  type ToolProvider, type ToolConnection, type ToolConnectionVersion, type SearchProvider,
} from '@/api/toolConnections'
import { skillsApi, type SkillVersion } from '@/api/skills'
import { Plus, Loader2, X } from 'lucide-react'

interface ProviderFormValues {
  name: string
  kind: string
}

export default function ToolConnectionsPage() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [expandedProviderId, setExpandedProviderId] = useState<string | null>(null)
  const [showCreateProvider, setShowCreateProvider] = useState(false)
  const [createError, setCreateError] = useState('')
  const { register, handleSubmit, reset } = useForm<ProviderFormValues>({
    defaultValues: { kind: 'search' },
  })

  const { data: providers, isLoading, isError, refetch } = useQuery({
    queryKey: ['tool-providers'],
    queryFn: () => toolConnectionsApi.listProviders().then(res => res.items),
  })

  const createProviderMut = useMutation({
    mutationFn: (data: ProviderFormValues) => toolConnectionsApi.createProvider(data.name, data.kind),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tool-providers'] })
      setShowCreateProvider(false)
      reset({ kind: 'search' })
      setCreateError('')
    },
    onError: (err: unknown) => {
      const e = err as { detail?: string; message?: string }
      setCreateError(e?.detail || e?.message || t('toolConnections.load_failed'))
    },
  })

  if (isError) {
    return (
      <div className="bg-red-50 border border-red-200 rounded-lg p-4 text-sm text-red-600" role="alert" data-testid="tool-connections-error">
        <p>{t('toolConnections.load_failed')}</p>
        <button type="button" onClick={() => refetch()} className="mt-2 px-3 py-1 text-xs border border-red-300 rounded hover:bg-red-100">
          {t('toolConnections.retry')}
        </button>
      </div>
    )
  }

  return (
    <div data-testid="tool-connections-page">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-xl font-semibold">{t('toolConnections.title')}</h2>
        <button type="button" onClick={() => setShowCreateProvider(true)}
          className="flex items-center gap-2 bg-black text-white px-4 py-2 rounded-lg text-sm" data-testid="create-provider-button">
          <Plus size={14} /> {t('toolConnections.create_provider')}
        </button>
      </div>

      {isLoading ? (
        <p className="text-gray-400 text-sm" data-testid="tool-connections-loading">{t('common.loading', '加载中…')}</p>
      ) : providers && providers.length === 0 ? (
        <p className="text-sm text-gray-400" data-testid="tool-providers-empty">{t('toolConnections.empty_providers')}</p>
      ) : (
        <div className="grid gap-3" data-testid="tool-providers-list">
          {(providers ?? []).map(provider => (
            <ProviderCard
              key={provider.id}
              provider={provider}
              expanded={expandedProviderId === provider.id}
              onToggle={() => setExpandedProviderId(prev => (prev === provider.id ? null : provider.id))}
            />
          ))}
        </div>
      )}

      <div className="mt-8 border-t pt-6">
        <SkillsSection />
      </div>

      {showCreateProvider && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={() => setShowCreateProvider(false)}>
          <div className="bg-white rounded-lg shadow-lg p-6 w-[420px]" onClick={e => e.stopPropagation()}>
            <div className="flex justify-between items-center mb-4">
              <h3 className="font-semibold">{t('toolConnections.create_provider')}</h3>
              <button type="button" onClick={() => setShowCreateProvider(false)} className="text-gray-400 hover:text-black"><X size={16} /></button>
            </div>
            {createError && <div className="mb-3 p-2.5 bg-red-50 border border-red-200 rounded-lg text-red-600 text-sm">{createError}</div>}
            <form onSubmit={handleSubmit(d => createProviderMut.mutate(d))} className="space-y-3">
              <div>
                <label className="block text-sm font-medium mb-1">{t('toolConnections.provider_name')} *</label>
                <input {...register('name', { required: true })} className="w-full border rounded-lg px-3 py-2 text-sm" data-testid="provider-name-input" />
              </div>
              <div>
                <label className="block text-sm font-medium mb-1">{t('toolConnections.provider_kind')} *</label>
                <select {...register('kind', { required: true })} className="w-full border rounded-lg px-3 py-2 text-sm" data-testid="provider-kind-select">
                  {PROVIDER_KINDS.map(kind => <option key={kind} value={kind}>{kind}</option>)}
                </select>
              </div>
              <div className="flex justify-end gap-3 pt-2">
                <button type="button" onClick={() => setShowCreateProvider(false)} className="px-4 py-2 border rounded-lg text-sm">{t('toolConnections.cancel')}</button>
                <button type="submit" disabled={createProviderMut.isPending} className="flex items-center gap-1.5 px-4 py-2 bg-black text-white rounded-lg text-sm disabled:opacity-50" data-testid="submit-create-provider">
                  {createProviderMut.isPending && <Loader2 size={13} className="animate-spin" />}{t('toolConnections.save')}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  )
}

function ProviderCard({ provider, expanded, onToggle }: { provider: ToolProvider; expanded: boolean; onToggle: () => void }) {
  const { t } = useTranslation()
  const qc = useQueryClient()

  const { data: connections, isLoading: connectionsLoading } = useQuery({
    queryKey: ['tool-connections', provider.id],
    queryFn: () => toolConnectionsApi.listConnections(provider.id).then(res => res.items),
    enabled: expanded,
  })

  const createConnectionMut = useMutation({
    mutationFn: () => toolConnectionsApi.createConnection(provider.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tool-connections', provider.id] })
    },
  })

  return (
    <div className="bg-white border rounded-lg p-4" data-testid={`provider-card-${provider.id}`}>
      <button type="button" onClick={onToggle} className="w-full text-left text-sm flex items-center justify-between">
        <span>
          <span className="font-semibold">{provider.name}</span>
          <span className="ml-2 text-xs text-gray-500">{provider.kind} · {provider.status}</span>
        </span>
      </button>

      {expanded && (
        <div className="mt-3 border-t pt-3" data-testid={`provider-detail-${provider.id}`}>
          <div className="flex items-center justify-between mb-2">
            <h4 className="text-sm font-medium">{t('toolConnections.connections')}</h4>
            <button type="button" onClick={() => createConnectionMut.mutate()}
              disabled={createConnectionMut.isPending}
              className="flex items-center gap-1 px-2.5 py-1 border rounded text-xs hover:bg-gray-50 disabled:opacity-50" data-testid={`create-connection-${provider.id}`}>
              <Plus size={12} /> {t('toolConnections.create_connection')}
            </button>
          </div>
          {connectionsLoading ? (
            <p className="text-xs text-gray-400">{t('common.loading', '加载中…')}</p>
          ) : connections && connections.length === 0 ? (
            <p className="text-xs text-gray-400" data-testid={`connections-empty-${provider.id}`}>{t('toolConnections.empty_connections')}</p>
          ) : (
            <ul className="space-y-2">
              {(connections ?? []).map(conn => (
                <ConnectionRow key={conn.id} connection={conn} providerKind={provider.kind} />
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

interface VersionFormValues {
  endpoint?: string
  audience?: string
  scopes_str?: string
  credential_reference?: string
  domains_str?: string
  search_provider?: SearchProvider
}

function extractErrorMessage(err: unknown, fallback: string): string {
  const e = err as { detail?: string; message?: string }
  return e?.detail || e?.message || fallback
}

/** Field set shared by the create-version and edit-version forms — the
 * fields a version needs depend only on its provider kind, not on whether
 * it's being created or edited. */
function ConnectionVersionFields({
  providerKind, register, setValue, credentialHint,
}: {
  providerKind: string
  register: UseFormRegister<VersionFormValues>
  setValue: (name: 'endpoint', value: string) => void
  credentialHint?: string
}) {
  const { t } = useTranslation()
  return (
    <>
      {providerKind === 'search' && (
        <select {...register('search_provider')}
          onChange={e => {
            const preset = SEARCH_PROVIDER_PRESETS[e.target.value as SearchProvider]
            if (preset?.endpoint) setValue('endpoint', preset.endpoint)
          }}
          className="w-full border rounded px-2 py-1 bg-white" data-testid="version-search-provider-select">
          {(Object.keys(SEARCH_PROVIDER_PRESETS) as SearchProvider[]).map(key => (
            <option key={key} value={key}>{t(SEARCH_PROVIDER_PRESETS[key].labelKey, SEARCH_PROVIDER_PRESETS[key].label)}</option>
          ))}
        </select>
      )}
      {providerKind !== 'playwright' && (
        <input {...register('endpoint')} placeholder={t('toolConnections.endpoint')} className="w-full border rounded px-2 py-1" data-testid="version-endpoint-input" />
      )}
      {providerKind === 'external_mcp' && (
        <>
          <input {...register('audience')} placeholder={t('toolConnections.audience')} className="w-full border rounded px-2 py-1" />
          <textarea {...register('scopes_str')} placeholder={t('toolConnections.scopes')} rows={2} className="w-full border rounded px-2 py-1 font-mono" />
        </>
      )}
      {providerKind !== 'playwright' && (
        <input {...register('credential_reference')} type="password"
          placeholder={credentialHint ?? ((providerKind === 'search' || providerKind === 'browser_use') ? t('toolConnections.api_key') : t('toolConnections.credential_reference'))}
          className="w-full border rounded px-2 py-1" />
      )}
      {providerKind !== 'search' && providerKind !== 'browser_use' && (
        <textarea {...register('domains_str')} placeholder={t('toolConnections.allowlist_domains')} rows={2} className="w-full border rounded px-2 py-1 font-mono" />
      )}
    </>
  )
}

/** Prefer the admin's own name for a connection (set via renameConnection);
 * otherwise it has no name of its own, so fall back to what its active
 * version is actually pointed at — the configured search preset, then the
 * raw endpoint, then the provider kind (e.g. Playwright, which has no
 * endpoint at all) — so two connections of the same kind are never both
 * shown as an indistinguishable opaque id. */
function connectionLabel(connection: ToolConnection, providerKind: string, t: TFunction): string {
  if (connection.name) return connection.name
  if (connection.active_version_search_provider) {
    const preset = SEARCH_PROVIDER_PRESETS[connection.active_version_search_provider]
    return t(preset.labelKey, preset.label)
  }
  if (connection.active_version_endpoint) {
    return connection.active_version_endpoint
  }
  return t(`toolConnections.provider_kind_${providerKind}`, providerKind)
}

function ConnectionRow({ connection, providerKind }: { connection: ToolConnection; providerKind: string }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [expanded, setExpanded] = useState(false)
  const [showCreateVersion, setShowCreateVersion] = useState(false)
  const [approvingVersion, setApprovingVersion] = useState<ToolConnectionVersion | null>(null)
  const [testResult, setTestResult] = useState<Record<string, { status: string; detail: string }>>({})
  const [pinResult, setPinResult] = useState<Record<string, string>>({})
  const [error, setError] = useState('')
  const [editingVersionId, setEditingVersionId] = useState<string | null>(null)
  const [editingName, setEditingName] = useState(false)
  const [nameDraft, setNameDraft] = useState('')
  const { register, handleSubmit, reset, setValue } = useForm<VersionFormValues>()
  const editForm = useForm<VersionFormValues>()

  const renameMut = useMutation({
    mutationFn: (name: string) => toolConnectionsApi.renameConnection(connection.id, name),
    onSuccess: () => {
      setError('')
      qc.invalidateQueries({ queryKey: ['tool-connections'] })
      setEditingName(false)
    },
    onError: (err: unknown) => setError(extractErrorMessage(err, t('toolConnections.rename_failed'))),
  })

  const { data: versions, isLoading } = useQuery({
    queryKey: ['tool-connection-versions', connection.id],
    queryFn: () => toolConnectionsApi.listVersions(connection.id).then(res => res.items),
    enabled: expanded,
  })

  const createVersionMut = useMutation({
    mutationFn: (data: VersionFormValues) => toolConnectionsApi.createVersion({
      connection_id: connection.id,
      endpoint: data.endpoint || undefined,
      audience: data.audience || undefined,
      scopes: data.scopes_str ? data.scopes_str.split('\n').map(s => s.trim()).filter(Boolean) : undefined,
      credential_reference: data.credential_reference || undefined,
      allowlists: data.domains_str
        ? { domains: data.domains_str.split('\n').map(s => s.trim()).filter(Boolean) }
        : undefined,
      search_provider: providerKind === 'search' ? data.search_provider : undefined,
    }),
    onSuccess: () => {
      setError('')
      qc.invalidateQueries({ queryKey: ['tool-connection-versions', connection.id] })
      setShowCreateVersion(false)
      reset()
    },
    onError: (err: unknown) => setError(extractErrorMessage(err, t('toolConnections.load_failed'))),
  })

  const updateVersionMut = useMutation({
    mutationFn: ({ versionId, data }: { versionId: string; data: VersionFormValues }) =>
      toolConnectionsApi.updateVersion(versionId, {
        endpoint: data.endpoint || undefined,
        audience: data.audience || undefined,
        scopes: data.scopes_str ? data.scopes_str.split('\n').map(s => s.trim()).filter(Boolean) : undefined,
        credential_reference: data.credential_reference || undefined,
        allowlists: data.domains_str
          ? { domains: data.domains_str.split('\n').map(s => s.trim()).filter(Boolean) }
          : undefined,
        search_provider: providerKind === 'search' ? data.search_provider : undefined,
      }),
    onSuccess: () => {
      setError('')
      qc.invalidateQueries({ queryKey: ['tool-connection-versions', connection.id] })
      setEditingVersionId(null)
      editForm.reset()
    },
    onError: (err: unknown) => setError(extractErrorMessage(err, t('toolConnections.update_failed'))),
  })

  const deleteVersionMut = useMutation({
    mutationFn: (versionId: string) => toolConnectionsApi.deleteVersion(versionId),
    onSuccess: () => {
      setError('')
      qc.invalidateQueries({ queryKey: ['tool-connection-versions', connection.id] })
    },
    onError: (err: unknown) => setError(extractErrorMessage(err, t('toolConnections.delete_failed'))),
  })

  const openEditForm = (v: ToolConnectionVersion) => {
    editForm.reset({
      endpoint: v.endpoint ?? '',
      audience: v.audience ?? '',
      scopes_str: v.scopes.join('\n'),
      credential_reference: '',
      domains_str: ((v.allowlists?.domains as string[] | undefined) ?? []).join('\n'),
      search_provider: v.search_provider ?? 'generic',
    })
    setEditingVersionId(v.id)
  }

  const testMut = useMutation({
    mutationFn: (versionId: string) => toolConnectionsApi.testVersion(versionId),
    onSuccess: (res, versionId) => {
      setError('')
      setTestResult(prev => ({ ...prev, [versionId]: res }))
      qc.invalidateQueries({ queryKey: ['tool-connection-versions', connection.id] })
    },
    onError: (err: unknown) => setError(extractErrorMessage(err, t('toolConnections.load_failed'))),
  })

  const approveMut = useMutation({
    mutationFn: (versionId: string) => toolConnectionsApi.approveVersion(versionId),
    onSuccess: () => {
      setError('')
      qc.invalidateQueries({ queryKey: ['tool-connection-versions', connection.id] })
      setApprovingVersion(null)
    },
    onError: (err: unknown) => setError(extractErrorMessage(err, t('toolConnections.load_failed'))),
  })

  const activateMut = useMutation({
    mutationFn: (versionId: string) => toolConnectionsApi.activateVersion(connection.id, versionId),
    onSuccess: () => {
      setError('')
      qc.invalidateQueries({ queryKey: ['tool-connections'] })
      qc.invalidateQueries({ queryKey: ['tool-connection-versions', connection.id] })
    },
    onError: (err: unknown) => setError(extractErrorMessage(err, t('toolConnections.load_failed'))),
  })

  const [tokenForm, setTokenForm] = useState<Record<string, boolean>>({})
  const { register: registerToken, handleSubmit: handleTokenSubmit, reset: resetToken } = useForm<{
    access_token: string; expires_in_seconds: string; scope_str: string
  }>()

  const openTokenForm = (versionId: string) => {
    resetToken()
    setTokenForm({ [versionId]: true })
  }
  const closeTokenForm = () => {
    setTokenForm({})
    resetToken()
  }

  const pinSchemaMut = useMutation({
    mutationFn: (versionId: string) => toolConnectionsApi.pinMcpSchema(versionId),
    onSuccess: (res, versionId) => {
      setError('')
      setPinResult(prev => ({ ...prev, [versionId]: t('toolConnections.pin_schema_result', { count: res.tool_count }) }))
    },
    onError: (err: unknown) => setError(extractErrorMessage(err, t('toolConnections.load_failed'))),
  })

  const issueTokenMut = useMutation({
    mutationFn: ({ versionId, data }: { versionId: string; data: { access_token: string; expires_in_seconds: string; scope_str: string } }) =>
      toolConnectionsApi.issueMcpToken(versionId, {
        access_token: data.access_token,
        expires_in_seconds: Number(data.expires_in_seconds),
        scope: data.scope_str.split('\n').map(s => s.trim()).filter(Boolean),
      }),
    onSuccess: () => {
      setError('')
      closeTokenForm()
    },
    onError: (err: unknown) => setError(extractErrorMessage(err, t('toolConnections.load_failed'))),
  })

  const canTest = (LIVE_PROVIDER_KINDS as readonly string[]).includes(providerKind)

  return (
    <li className="border rounded p-2 text-xs" data-testid={`connection-row-${connection.id}`}>
      <div className="w-full flex items-center justify-between">
        <button type="button" onClick={() => setExpanded(e => !e)} className="flex-1 text-left flex items-center">
          <span className="font-medium">{connectionLabel(connection, providerKind, t)}</span>
          <span className="ml-1.5 font-mono text-gray-400 text-[10px]">#{connection.id.slice(0, 6)}</span>
          <span className="ml-2">
            {connection.active_version_id
              ? <span className="text-green-600">{t('toolConnections.activated')}</span>
              : <span className="text-gray-400">{t('toolConnections.none')}</span>}
          </span>
        </button>
        <button type="button"
          onClick={() => { setNameDraft(connection.name ?? ''); setEditingName(v => !v) }}
          data-testid={`rename-connection-${connection.id}`}
          className="ml-2 px-1.5 py-0.5 text-[10px] border rounded hover:bg-gray-50 shrink-0">
          {t('toolConnections.rename')}
        </button>
      </div>

      {editingName && (
        <div className="mt-1.5 flex items-center gap-1">
          <input value={nameDraft} onChange={e => setNameDraft(e.target.value)}
            placeholder={t('toolConnections.rename_placeholder')}
            data-testid={`rename-connection-input-${connection.id}`}
            className="border rounded px-1.5 py-0.5 text-xs flex-1" />
          <button type="button" disabled={renameMut.isPending || !nameDraft.trim()}
            onClick={() => renameMut.mutate(nameDraft)}
            data-testid={`save-rename-connection-${connection.id}`}
            className="px-2 py-0.5 bg-black text-white rounded disabled:opacity-50">
            {t('toolConnections.save')}
          </button>
          <button type="button" onClick={() => setEditingName(false)} className="px-2 py-0.5 border rounded">
            {t('toolConnections.cancel')}
          </button>
        </div>
      )}

      {expanded && (
        <div className="mt-2 border-t pt-2" data-testid={`connection-detail-${connection.id}`}>
          {error && <p className="text-red-600 mb-2" role="alert" data-testid={`connection-error-${connection.id}`}>{error}</p>}
          <div className="flex items-center justify-between mb-2">
            <h5 className="font-medium">{t('toolConnections.versions')}</h5>
            <button type="button" onClick={() => setShowCreateVersion(v => !v)}
              className="flex items-center gap-1 px-2 py-0.5 border rounded hover:bg-gray-50" data-testid={`create-version-${connection.id}`}>
              <Plus size={11} /> {t('toolConnections.create_version')}
            </button>
          </div>

          {!canTest && (
            <p className="text-gray-400 mb-2" data-testid={`no-health-probe-${connection.id}`}>{t('toolConnections.no_health_probe')}</p>
          )}

          {showCreateVersion && (
            <form onSubmit={handleSubmit(d => createVersionMut.mutate(d))} className="space-y-2 mb-3 p-2 bg-gray-50 rounded">
              {/* Only external_mcp actually uses every field (OAuth audience/scopes plus a
                  domain allowlist); search and playwright each use a narrow subset — showing
                  the full generic set for those kinds is what made this form confusing. */}
              <ConnectionVersionFields providerKind={providerKind} register={register}
                setValue={(name, value) => setValue(name, value)} />
              <button type="submit" disabled={createVersionMut.isPending} className="px-3 py-1 bg-black text-white rounded disabled:opacity-50" data-testid="submit-create-version">
                {t('toolConnections.save')}
              </button>
            </form>
          )}

          {isLoading ? (
            <p className="text-gray-400">{t('common.loading', '加载中…')}</p>
          ) : versions && versions.length === 0 ? (
            <p className="text-gray-400" data-testid={`versions-empty-${connection.id}`}>{t('toolConnections.empty_versions')}</p>
          ) : (
            <ul className="space-y-1.5">
              {(versions ?? []).map(v => (
                <li key={v.id} className="border rounded p-1.5" data-testid={`version-row-${v.id}`}>
                  <div className="flex items-center justify-between">
                    <span>
                      v{v.version_no} ·{' '}
                      <span className={v.approval_status === 'approved' ? 'text-green-600' : v.approval_status === 'rejected' ? 'text-red-600' : 'text-amber-600'}>
                        {t(`toolConnections.approval_${v.approval_status}`)}
                      </span>
                      {' · '}
                      <span className={v.health_status === 'healthy' ? 'text-green-600' : v.health_status === 'unhealthy' ? 'text-red-600' : 'text-gray-400'}>
                        {t(`toolConnections.health_${v.health_status}`)}
                      </span>
                      {connection.active_version_id === v.id && (
                        <span className="ml-1 bg-black text-white px-1.5 py-0.5 rounded text-[10px]">{t('toolConnections.activated')}</span>
                      )}
                    </span>
                    <span className="flex gap-1">
                      {canTest && v.approval_status === 'approved' && (
                        <button type="button" onClick={() => testMut.mutate(v.id)} disabled={testMut.isPending}
                          className="px-2 py-0.5 border rounded hover:bg-gray-50 disabled:opacity-50" data-testid={`test-version-${v.id}`}>
                          {t('toolConnections.test')}
                        </button>
                      )}
                      {v.approval_status === 'pending' && (
                        <>
                          <button type="button" onClick={() => (editingVersionId === v.id ? setEditingVersionId(null) : openEditForm(v))}
                            className="px-2 py-0.5 border rounded hover:bg-gray-50" data-testid={`edit-version-${v.id}`}>
                            {t('toolConnections.edit_version')}
                          </button>
                          <button type="button" onClick={() => setApprovingVersion(v)}
                            className="px-2 py-0.5 border rounded hover:bg-gray-50 text-blue-600" data-testid={`approve-version-${v.id}`}>
                            {t('toolConnections.approve')}
                          </button>
                          <button type="button"
                            onClick={() => { if (window.confirm(t('toolConnections.delete_version_confirm'))) deleteVersionMut.mutate(v.id) }}
                            disabled={deleteVersionMut.isPending}
                            className="px-2 py-0.5 border rounded hover:bg-gray-50 text-red-600 disabled:opacity-50" data-testid={`delete-version-${v.id}`}>
                            {t('toolConnections.delete_version')}
                          </button>
                        </>
                      )}
                      {v.approval_status === 'approved' && connection.active_version_id !== v.id && (
                        <button type="button" onClick={() => activateMut.mutate(v.id)} disabled={activateMut.isPending}
                          className="px-2 py-0.5 border rounded hover:bg-gray-50" data-testid={`activate-version-${v.id}`}>
                          {t('toolConnections.activate')}
                        </button>
                      )}
                      {providerKind === 'external_mcp' && v.approval_status === 'approved' && (
                        <>
                          <button type="button" onClick={() => pinSchemaMut.mutate(v.id)} disabled={pinSchemaMut.isPending}
                            className="px-2 py-0.5 border rounded hover:bg-gray-50 disabled:opacity-50" data-testid={`pin-schema-${v.id}`}>
                            {t('toolConnections.pin_mcp_schema')}
                          </button>
                          <button type="button" onClick={() => (tokenForm[v.id] ? closeTokenForm() : openTokenForm(v.id))}
                            className="px-2 py-0.5 border rounded hover:bg-gray-50" data-testid={`issue-token-toggle-${v.id}`}>
                            {t('toolConnections.issue_mcp_token')}
                          </button>
                        </>
                      )}
                    </span>
                  </div>
                  {testResult[v.id] && (
                    <p className={`mt-1 ${testResult[v.id].status === 'healthy' ? 'text-green-600' : 'text-amber-600'}`} data-testid={`test-result-${v.id}`}>
                      {testResult[v.id].detail}
                    </p>
                  )}
                  {pinResult[v.id] && (
                    <p className="mt-1 text-gray-500" data-testid={`pin-result-${v.id}`}>{pinResult[v.id]}</p>
                  )}
                  {editingVersionId === v.id && (
                    <form onSubmit={editForm.handleSubmit(data => updateVersionMut.mutate({ versionId: v.id, data }))}
                      className="mt-2 space-y-2 p-2 bg-gray-50 rounded" data-testid={`edit-version-form-${v.id}`}>
                      <ConnectionVersionFields providerKind={providerKind} register={editForm.register}
                        setValue={(name, value) => editForm.setValue(name, value)}
                        credentialHint={t('toolConnections.credential_reference_edit_hint')} />
                      <button type="submit" disabled={updateVersionMut.isPending}
                        className="px-3 py-1 bg-black text-white rounded disabled:opacity-50" data-testid={`submit-edit-version-${v.id}`}>
                        {t('toolConnections.save_changes')}
                      </button>
                    </form>
                  )}
                  {tokenForm[v.id] && (
                    <form onSubmit={handleTokenSubmit(data => issueTokenMut.mutate({ versionId: v.id, data }))}
                      className="mt-2 space-y-1.5 p-2 bg-gray-50 rounded" data-testid={`issue-token-form-${v.id}`}>
                      <input {...registerToken('access_token', { required: true })} type="password"
                        placeholder={t('toolConnections.mcp_token_placeholder')} className="w-full border rounded px-2 py-1" data-testid={`mcp-token-input-${v.id}`} />
                      <input {...registerToken('expires_in_seconds', { required: true })} type="number"
                        placeholder={t('toolConnections.mcp_token_ttl_placeholder')} className="w-full border rounded px-2 py-1" />
                      <textarea {...registerToken('scope_str')} rows={2}
                        placeholder={t('toolConnections.mcp_scope_placeholder')} className="w-full border rounded px-2 py-1 font-mono" />
                      <button type="submit" disabled={issueTokenMut.isPending}
                        className="px-3 py-1 bg-black text-white rounded disabled:opacity-50" data-testid={`submit-issue-token-${v.id}`}>
                        {t('toolConnections.save')}
                      </button>
                    </form>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {approvingVersion && (
        <VersionApprovalDialog
          version={approvingVersion}
          onCancel={() => setApprovingVersion(null)}
          onConfirm={() => approveMut.mutate(approvingVersion.id)}
          isPending={approveMut.isPending}
        />
      )}
    </li>
  )
}

function VersionApprovalDialog({
  version, onCancel, onConfirm, isPending,
}: {
  version: ToolConnectionVersion
  onCancel: () => void
  onConfirm: () => void
  isPending: boolean
}) {
  const { t } = useTranslation()
  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={onCancel}>
      <div className="bg-white rounded-lg shadow-lg p-6 w-[480px]" onClick={e => e.stopPropagation()} data-testid="approve-version-dialog">
        <h3 className="font-semibold mb-2">{t('toolConnections.approve_confirm_title')}</h3>
        <p className="text-sm text-gray-500 mb-3">{t('toolConnections.approve_confirm_body')}</p>
        <dl className="grid grid-cols-2 gap-1 text-xs text-gray-600 mb-4 bg-gray-50 rounded p-2">
          <dt>{t('toolConnections.endpoint')}</dt><dd className="font-mono break-all">{version.endpoint || '—'}</dd>
          <dt>{t('toolConnections.audience')}</dt><dd className="font-mono break-all">{version.audience || '—'}</dd>
          <dt>{t('toolConnections.scopes')}</dt><dd className="font-mono break-all">{version.scopes.join(', ') || '—'}</dd>
          <dt>{t('toolConnections.allowlist_domains')}</dt>
          <dd className="font-mono break-all">{((version.allowlists?.domains as string[] | undefined) ?? []).join(', ') || '—'}</dd>
        </dl>
        <div className="flex justify-end gap-3">
          <button type="button" onClick={onCancel} className="px-4 py-2 border rounded-lg text-sm">{t('toolConnections.cancel')}</button>
          <button type="button" onClick={onConfirm} disabled={isPending}
            className="px-4 py-2 bg-black text-white rounded-lg text-sm disabled:opacity-50" data-testid="confirm-approve-version">
            {t('toolConnections.confirm')}
          </button>
        </div>
      </div>
    </div>
  )
}

function SkillsSection() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [showCreatePackage, setShowCreatePackage] = useState(false)
  const [packageError, setPackageError] = useState('')
  const { register, handleSubmit, reset } = useForm<{ name: string }>()

  const { data: packages, isLoading } = useQuery({
    queryKey: ['skill-packages'],
    queryFn: () => skillsApi.listPackages().then(res => res.items),
  })

  const createPackageMut = useMutation({
    mutationFn: (data: { name: string }) => skillsApi.createPackage(data.name),
    onSuccess: () => {
      setPackageError('')
      qc.invalidateQueries({ queryKey: ['skill-packages'] })
      setShowCreatePackage(false)
      reset()
    },
    onError: (err: unknown) => setPackageError(extractErrorMessage(err, t('toolConnections.load_failed'))),
  })

  return (
    <div data-testid="skills-section">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-lg font-semibold">{t('toolConnections.skills_title')}</h3>
        <button type="button" onClick={() => setShowCreatePackage(v => !v)}
          className="flex items-center gap-2 border px-3 py-1.5 rounded-lg text-sm hover:bg-gray-50" data-testid="create-skill-package-button">
          <Plus size={13} /> {t('toolConnections.create_skill_package')}
        </button>
      </div>
      {packageError && <p className="text-red-600 text-sm mb-2" role="alert" data-testid="skills-section-error">{packageError}</p>}
      {showCreatePackage && (
        <form onSubmit={handleSubmit(d => createPackageMut.mutate(d))} className="flex gap-2 mb-3">
          <input {...register('name', { required: true })} placeholder={t('toolConnections.skill_package_name')}
            className="border rounded-lg px-3 py-1.5 text-sm flex-1" data-testid="skill-package-name-input" />
          <button type="submit" disabled={createPackageMut.isPending} className="px-4 py-1.5 bg-black text-white rounded-lg text-sm disabled:opacity-50" data-testid="submit-create-skill-package">
            {t('toolConnections.save')}
          </button>
        </form>
      )}
      {isLoading ? (
        <p className="text-gray-400 text-sm">{t('common.loading', '加载中…')}</p>
      ) : packages && packages.length === 0 ? (
        <p className="text-sm text-gray-400" data-testid="skill-packages-empty">{t('toolConnections.empty_skill_packages')}</p>
      ) : (
        <div className="grid gap-3">
          {(packages ?? []).map(pkg => <SkillPackageCard key={pkg.id} packageId={pkg.id} packageName={pkg.name} />)}
        </div>
      )}
    </div>
  )
}

function SkillPackageCard({ packageId, packageName }: { packageId: string; packageName: string }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [expanded, setExpanded] = useState(false)
  const [showCreateVersion, setShowCreateVersion] = useState(false)
  const [showUploadVersion, setShowUploadVersion] = useState(false)
  const [uploadFile, setUploadFile] = useState<File | null>(null)
  const [uploadResult, setUploadResult] = useState<string | null>(null)
  const [approvingVersion, setApprovingVersion] = useState<SkillVersion | null>(null)
  const [versionError, setVersionError] = useState('')
  const { register, handleSubmit, reset } = useForm<{ manifest_json: string; signatures_json: string }>()

  const { data: versions, isLoading } = useQuery({
    queryKey: ['skill-versions', packageId],
    queryFn: () => skillsApi.listVersions(packageId).then(res => res.items),
    enabled: expanded,
  })

  const createVersionMut = useMutation({
    mutationFn: (data: { manifest_json: string; signatures_json: string }) =>
      skillsApi.createVersion(packageId, JSON.parse(data.manifest_json), JSON.parse(data.signatures_json)),
    onSuccess: () => {
      setVersionError('')
      qc.invalidateQueries({ queryKey: ['skill-versions', packageId] })
      setShowCreateVersion(false)
      reset()
    },
    onError: (err: unknown) => setVersionError(extractErrorMessage(err, t('toolConnections.load_failed'))),
  })

  const uploadVersionMut = useMutation({
    mutationFn: (file: File) => skillsApi.uploadVersion(packageId, file),
    onSuccess: result => {
      setVersionError('')
      setUploadResult(result.id)
      qc.invalidateQueries({ queryKey: ['skill-versions', packageId] })
      setUploadFile(null)
    },
    onError: (err: unknown) => setVersionError(extractErrorMessage(err, t('toolConnections.upload_failed'))),
  })

  const approveMut = useMutation({
    mutationFn: (versionId: string) => skillsApi.approveVersion(versionId),
    onSuccess: () => {
      setVersionError('')
      qc.invalidateQueries({ queryKey: ['skill-versions', packageId] })
      setApprovingVersion(null)
    },
    onError: (err: unknown) => setVersionError(extractErrorMessage(err, t('toolConnections.load_failed'))),
  })

  const [editingName, setEditingName] = useState(false)
  const [nameDraft, setNameDraft] = useState('')

  const renameMut = useMutation({
    mutationFn: (name: string) => skillsApi.renamePackage(packageId, name),
    onSuccess: () => {
      setVersionError('')
      qc.invalidateQueries({ queryKey: ['skill-packages'] })
      setEditingName(false)
    },
    onError: (err: unknown) => setVersionError(extractErrorMessage(err, t('toolConnections.rename_failed'))),
  })

  return (
    <div className="bg-white border rounded-lg p-4" data-testid={`skill-package-card-${packageId}`}>
      <div className="flex items-center justify-between">
        <button type="button" onClick={() => setExpanded(e => !e)} className="flex-1 text-left text-sm font-semibold">
          {packageName}
        </button>
        <button type="button" onClick={() => { setNameDraft(packageName); setEditingName(v => !v) }}
          data-testid={`rename-skill-package-${packageId}`}
          className="ml-2 px-1.5 py-0.5 text-[10px] border rounded hover:bg-gray-50 shrink-0">
          {t('toolConnections.rename')}
        </button>
      </div>
      {editingName && (
        <div className="mt-1.5 flex items-center gap-1">
          <input value={nameDraft} onChange={e => setNameDraft(e.target.value)}
            placeholder={t('toolConnections.rename_placeholder')}
            data-testid={`rename-skill-package-input-${packageId}`}
            className="border rounded px-1.5 py-0.5 text-xs flex-1" />
          <button type="button" disabled={renameMut.isPending || !nameDraft.trim()}
            onClick={() => renameMut.mutate(nameDraft)}
            data-testid={`save-rename-skill-package-${packageId}`}
            className="px-2 py-0.5 text-xs bg-black text-white rounded disabled:opacity-50">
            {t('toolConnections.save')}
          </button>
          <button type="button" onClick={() => setEditingName(false)} className="px-2 py-0.5 text-xs border rounded">
            {t('toolConnections.cancel')}
          </button>
        </div>
      )}
      {expanded && (
        <div className="mt-3 border-t pt-3 text-xs" data-testid={`skill-package-detail-${packageId}`}>
          {versionError && <p className="text-red-600 mb-2" role="alert" data-testid={`skill-version-error-${packageId}`}>{versionError}</p>}
          <div className="flex items-center justify-between mb-2">
            <h5 className="font-medium">{t('toolConnections.skill_versions')}</h5>
            <span className="flex gap-1">
              <button type="button" onClick={() => setShowUploadVersion(v => !v)}
                className="flex items-center gap-1 px-2 py-0.5 border rounded hover:bg-gray-50" data-testid={`upload-skill-version-${packageId}`}>
                <Plus size={11} /> {t('toolConnections.upload_skill_version')}
              </button>
              <button type="button" onClick={() => setShowCreateVersion(v => !v)}
                className="flex items-center gap-1 px-2 py-0.5 border rounded hover:bg-gray-50" data-testid={`create-skill-version-${packageId}`}>
                <Plus size={11} /> {t('toolConnections.create_skill_version')}
              </button>
            </span>
          </div>
          {showUploadVersion && (
            <div className="space-y-2 mb-3 p-2 bg-gray-50 rounded">
              <p className="text-gray-500">{t('toolConnections.upload_skill_hint')}</p>
              <input type="file" accept=".md,.zip" data-testid="skill-upload-file-input"
                onChange={e => setUploadFile(e.target.files?.[0] ?? null)} className="text-xs" />
              <button type="button" disabled={!uploadFile || uploadVersionMut.isPending}
                onClick={() => uploadFile && uploadVersionMut.mutate(uploadFile)}
                className="px-3 py-1 bg-black text-white rounded disabled:opacity-50" data-testid="submit-upload-skill-version">
                {t('toolConnections.upload')}
              </button>
              {uploadResult && (
                <p className="text-green-600" data-testid={`skill-upload-result-${packageId}`}>
                  {t('toolConnections.upload_scan_clean')}
                </p>
              )}
            </div>
          )}
          {showCreateVersion && (
            <form onSubmit={handleSubmit(d => createVersionMut.mutate(d))} className="space-y-2 mb-3 p-2 bg-gray-50 rounded">
              <textarea {...register('manifest_json', { required: true })} rows={4}
                placeholder={t('toolConnections.skill_manifest')} className="w-full border rounded px-2 py-1 font-mono" data-testid="skill-manifest-input" />
              <textarea {...register('signatures_json', { required: true })} rows={3}
                placeholder={t('toolConnections.skill_signatures')} className="w-full border rounded px-2 py-1 font-mono" data-testid="skill-signatures-input" />
              <button type="submit" disabled={createVersionMut.isPending} className="px-3 py-1 bg-black text-white rounded disabled:opacity-50" data-testid="submit-create-skill-version">
                {t('toolConnections.save')}
              </button>
            </form>
          )}
          {isLoading ? (
            <p className="text-gray-400">{t('common.loading', '加载中…')}</p>
          ) : versions && versions.length === 0 ? (
            <p className="text-gray-400" data-testid={`skill-versions-empty-${packageId}`}>{t('toolConnections.empty_skill_versions')}</p>
          ) : (
            <ul className="space-y-1.5">
              {(versions ?? []).map(v => (
                <li key={v.id} className="border rounded p-1.5 flex items-center justify-between" data-testid={`skill-version-row-${v.id}`}>
                  <span>
                    v{v.version_no} ·{' '}
                    <span className={v.approval_status === 'approved' ? 'text-green-600' : v.approval_status === 'rejected' ? 'text-red-600' : 'text-amber-600'}>
                      {t(`toolConnections.approval_${v.approval_status}`)}
                    </span>
                  </span>
                  {v.approval_status === 'pending' && (
                    <button type="button" onClick={() => setApprovingVersion(v)}
                      className="px-2 py-0.5 border rounded hover:bg-gray-50 text-blue-600" data-testid={`approve-skill-version-${v.id}`}>
                      {t('toolConnections.approve')}
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {approvingVersion && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={() => setApprovingVersion(null)}>
          <div className="bg-white rounded-lg shadow-lg p-6 w-[480px]" onClick={e => e.stopPropagation()} data-testid="approve-skill-version-dialog">
            <h3 className="font-semibold mb-2">{t('toolConnections.approve_confirm_title')}</h3>
            <p className="text-sm text-gray-500 mb-3">{t('toolConnections.skill_approve_confirm_body')}</p>
            <p className="text-xs font-mono bg-gray-50 rounded p-2 mb-4 break-all">{approvingVersion.canonical_hash}</p>
            <div className="flex justify-end gap-3">
              <button type="button" onClick={() => setApprovingVersion(null)} className="px-4 py-2 border rounded-lg text-sm">{t('toolConnections.cancel')}</button>
              <button type="button" onClick={() => approveMut.mutate(approvingVersion.id)} disabled={approveMut.isPending}
                className="px-4 py-2 bg-black text-white rounded-lg text-sm disabled:opacity-50" data-testid="confirm-approve-skill-version">
                {t('toolConnections.confirm')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
