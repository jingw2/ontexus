import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  agentToolsApi,
  type PublishedOntology,
  type ToolValidationResult,
} from '@/api/agentTools'
import { agentDetailApi, type AgentVersion } from '@/api/agentDetail'
import { agentExternalToolsApi, type ExternalToolCatalogItem } from '@/api/agentExternalTools'
import { agentSkillsApi, type SkillCatalogItem } from '@/api/agentSkills'
import ExternalToolCard, { type BoundExternalTool, type BoundSkill } from './ExternalToolCard'
import CapabilityDrawer from './CapabilityDrawer'
import { useOntologyToolSelection } from '@/pages/agents/shared/useOntologyToolSelection'
import OntologyToolSelector from '@/pages/agents/shared/OntologyToolSelector'

interface Props {
  agentId: string
  activeVersion: AgentVersion | null
  canEdit: boolean
  onSaved: (result: { version_no: number }) => void
  onDirtyChange: (dirty: boolean) => void
}

// Must match backend/app/services/agent/configuration.py exactly.
const DEFAULT_MAX_TOOL_ROUNDS = 5
const MAX_TOOL_ROUNDS_RANGE = [1, 20] as const

function clampToolRounds(value: number): number {
  const [lo, hi] = MAX_TOOL_ROUNDS_RANGE
  if (Number.isNaN(value)) return lo
  return Math.min(Math.max(Math.trunc(value), lo), hi)
}

export default function ToolConfigTab({ agentId, activeVersion, canEdit, onSaved, onDirtyChange }: Props) {
  const { t } = useTranslation()
  const [ontologies, setOntologies] = useState<PublishedOntology[]>([])
  const [maxToolRounds, setMaxToolRounds] = useState(String(DEFAULT_MAX_TOOL_ROUNDS))
  const {
    bindings, setBindings, toolsByOntology, error, setError,
    bindOntology, unbindOntology, toggleCategory, toggleTool, setToolCatalogLimit, setEntitySearchDepth,
  } = useOntologyToolSelection(ontologies)
  const [validation, setValidation] = useState<ToolValidationResult | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [externalBindings, setExternalBindings] = useState<BoundExternalTool[]>([])
  const [externalError, setExternalError] = useState('')
  const [skillBindings, setSkillBindings] = useState<BoundSkill[]>([])
  const [skillBindError, setSkillBindError] = useState('')

  const loadExternalBindings = useCallback(() => {
    if (!activeVersion) return
    agentExternalToolsApi.listBindings(agentId, activeVersion.id)
      .then(res => setExternalBindings(Array.isArray(res.items) ? res.items : []))
      .catch(() => setExternalError('AGENTS_EXTERNAL_BINDINGS_LOAD_FAILED'))
  }, [agentId, activeVersion])

  useEffect(() => { loadExternalBindings() }, [loadExternalBindings])

  const loadSkillBindings = useCallback(() => {
    if (!activeVersion) return
    agentSkillsApi.listBindings(agentId, activeVersion.id)
      .then(res => setSkillBindings(Array.isArray(res.items) ? res.items : []))
      .catch(() => setSkillBindError('AGENTS_SKILL_BINDINGS_LOAD_FAILED'))
  }, [agentId, activeVersion])

  useEffect(() => { loadSkillBindings() }, [loadSkillBindings])

  const bindExternal = useCallback(async (item: ExternalToolCatalogItem, alias: string) => {
    if (!activeVersion) return
    setExternalError('')
    try {
      await agentExternalToolsApi.bind(agentId, activeVersion.id,
        { tool_connection_version_id: item.tool_connection_version_id, alias })
      loadExternalBindings()
    } catch {
      setExternalError(t('agent.tools.bind_failed', '绑定失败（别名可能已被占用，或所选连接已失效）'))
    }
  }, [agentId, activeVersion, loadExternalBindings, t])

  const unbindExternal = useCallback(async (alias: string) => {
    if (!activeVersion) return
    setExternalError('')
    try {
      await agentExternalToolsApi.unbind(agentId, activeVersion.id, alias)
      loadExternalBindings()
    } catch {
      setExternalError(t('agent.tools.unbind_failed', '解绑失败'))
    }
  }, [agentId, activeVersion, loadExternalBindings, t])

  const bindSkillFn = useCallback(async (item: SkillCatalogItem, alias: string) => {
    if (!activeVersion) return
    setSkillBindError('')
    try {
      await agentSkillsApi.bind(agentId, activeVersion.id,
        { skill_version_id: item.skill_version_id, alias })
      loadSkillBindings()
    } catch {
      setSkillBindError(t('agent.tools.bind_failed', '绑定失败（别名可能已被占用，或所选连接已失效）'))
    }
  }, [agentId, activeVersion, loadSkillBindings, t])

  const unbindSkillFn = useCallback(async (alias: string) => {
    if (!activeVersion) return
    setSkillBindError('')
    try {
      await agentSkillsApi.unbind(agentId, activeVersion.id, alias)
      loadSkillBindings()
    } catch {
      setSkillBindError(t('agent.tools.unbind_failed', '解绑失败'))
    }
  }, [agentId, activeVersion, loadSkillBindings, t])

  useEffect(() => {
    let cancelled = false
    agentToolsApi.listPublishedOntologies()
      .then(res => { if (!cancelled) setOntologies(Array.isArray(res.items) ? res.items : []) })
      .catch(() => { if (!cancelled) setError('AGENTS_TOOLS_CATALOG_FAILED') })
    return () => { cancelled = true }
  }, [])

  // seed the working binding set from the active version's persisted bindings
  useEffect(() => {
    void Promise.resolve().then(() => {
      const persisted = activeVersion?.ontology_bindings ?? []
      setBindings(persisted.map(b => ({
        ontology_id: b.ontology_id,
        capabilities: Array.isArray(b.capabilities) ? b.capabilities : [],
        allowlists: b.allowlists ?? {},
        selected_tools: Array.isArray(b.selected_tools) ? b.selected_tools : [],
        // legacy bindings keep no enabled_categories until the user edits them
        ...(b.enabled_categories !== undefined && b.enabled_categories !== null
          ? { enabled_categories: b.enabled_categories }
          : {}),
        ...(b.tool_catalog_limit !== undefined && b.tool_catalog_limit !== null
          ? { tool_catalog_limit: b.tool_catalog_limit }
          : {}),
        ...(b.entity_search_depth !== undefined && b.entity_search_depth !== null
          ? { entity_search_depth: b.entity_search_depth }
          : {}),
      })))
      setMaxToolRounds(String(activeVersion?.max_tool_rounds ?? DEFAULT_MAX_TOOL_ROUNDS))
      setError('')
    })
    onDirtyChange(false)
  }, [activeVersion, onDirtyChange])

  // validate the proposed binding set whenever it changes
  useEffect(() => {
    if (!canEdit || bindings.length === 0) {
      // drop a stale result so an old conflict banner doesn't outlive the binding
      void Promise.resolve().then(() => setValidation(null))
      return
    }
    let cancelled = false
    agentToolsApi.validateAgentTools(agentId, { ontology_ids: bindings.map(b => b.ontology_id) })
      .then(result => { if (!cancelled) setValidation(result) })
      .catch(() => { if (!cancelled) setError('AGENTS_TOOLS_VALIDATION_FAILED') })
    return () => { cancelled = true }
  }, [agentId, bindings, canEdit])

  const persistedBindings = (activeVersion?.ontology_bindings ?? []).map(b => ({
    ontology_id: b.ontology_id,
    capabilities: Array.isArray(b.capabilities) ? b.capabilities : [],
    allowlists: b.allowlists ?? {},
    selected_tools: Array.isArray(b.selected_tools) ? b.selected_tools : [],
    ...(b.enabled_categories !== undefined && b.enabled_categories !== null
      ? { enabled_categories: b.enabled_categories }
      : {}),
    ...(b.tool_catalog_limit !== undefined && b.tool_catalog_limit !== null
      ? { tool_catalog_limit: b.tool_catalog_limit }
      : {}),
    ...(b.entity_search_depth !== undefined && b.entity_search_depth !== null
      ? { entity_search_depth: b.entity_search_depth }
      : {}),
  }))
  const persistedMaxToolRounds = String(activeVersion?.max_tool_rounds ?? DEFAULT_MAX_TOOL_ROUNDS)
  const dirty = JSON.stringify(bindings) !== JSON.stringify(persistedBindings)
    || maxToolRounds !== persistedMaxToolRounds
  useEffect(() => {
    onDirtyChange(dirty)
  }, [dirty, onDirtyChange])

  const save = useCallback(async () => {
    if (!activeVersion) return
    setSaving(true)
    setError('')
    try {
      const result = await agentDetailApi.saveVersion(agentId, {
        base_version_no: activeVersion.version_no,
        name: activeVersion.name,
        description: activeVersion.description ?? null,
        default_model_config_version_id: activeVersion.default_model_config_version_id ?? '',
        default_model_name: activeVersion.default_model_name ?? '',
        system_prompt: activeVersion.system_prompt ?? null,
        memory_settings: activeVersion.memory_settings ?? {},
        max_tool_rounds: clampToolRounds(Number(maxToolRounds)),
        application_state_schema_version_id: activeVersion.application_state_schema_version_id ?? null,
        change_note: t('agent.tools.change_note', '工具选择更新'),
        ontology_bindings: bindings,
      })
      onSaved(result)
    } catch (err) {
      const code = (err as { error?: { code?: string } })?.error?.code
      if (code === 'AGENT_VERSION_CONFLICT') setError(t('agent.tools.conflict', '检测到新版本，请刷新后重试'))
      else setError(t('agent.tools.save_failed', '保存失败'))
    } finally {
      setSaving(false)
    }
  }, [agentId, activeVersion, bindings, maxToolRounds, onSaved, t])

  return (
    <div className="p-6 space-y-6" data-testid="tool-config-tab">
      <div>
        <h3 className="text-sm font-medium text-gray-700 mb-2">{t('agent.tools.max_tool_rounds', '工具调用轮数上限')}</h3>
        <input type="number" min={MAX_TOOL_ROUNDS_RANGE[0]} max={MAX_TOOL_ROUNDS_RANGE[1]}
          value={maxToolRounds} disabled={!canEdit} data-testid="max-tool-rounds-input"
          onChange={e => setMaxToolRounds(e.target.value)}
          onBlur={() => setMaxToolRounds(String(clampToolRounds(Number(maxToolRounds))))}
          className="w-24 border rounded-lg px-3 py-2 text-sm disabled:bg-gray-50 disabled:text-gray-500" />
        <p className="text-xs text-gray-400 mt-1">
          {t('agent.tools.max_tool_rounds_hint',
             '最多支持 {{min}}-{{max}} 轮工具调用；超过后本轮对话会以 TOOL_ROUND_LIMIT 结束',
             { min: MAX_TOOL_ROUNDS_RANGE[0], max: MAX_TOOL_ROUNDS_RANGE[1] })}
        </p>
      </div>

      <div>
        <h3 className="text-sm font-medium text-gray-700 mb-2">{t('agent.tools.ontology_bindings', '本体绑定')}</h3>
        {bindings[0]?.selected_tools?.[0] && (
          <p className="text-xs text-gray-500 font-mono mb-2">
            {t('agent.tools.bound_mcp_tool', '已绑定工具')}: <span data-testid="journey-mcp-tool">{bindings[0].selected_tools[0]}</span>
          </p>
        )}
        {error && <p className="text-sm text-red-500 mb-2">{error}</p>}
        {/* OntologyToolSelector renders the published-ontology dropdown (data-testid="ontology-picker") and bound-ontology panels */}
        <OntologyToolSelector ontologies={ontologies} bindings={bindings} toolsByOntology={toolsByOntology}
          canEdit={canEdit} onBind={bindOntology} onUnbind={unbindOntology}
          onToggleCategory={toggleCategory} onToggleTool={toggleTool}
          onSetToolCatalogLimit={setToolCatalogLimit} onSetEntitySearchDepth={setEntitySearchDepth} error={error} />
      </div>

      {validation && (
        <div className={`border rounded-lg p-3 text-sm ${validation.valid ? 'bg-green-50 border-green-200 text-green-700' : 'bg-amber-50 border-amber-200 text-amber-700'}`}>
          <p>{validation.valid
            ? t('agent.tools.valid', '绑定配置有效')
            : t('agent.tools.invalid', '绑定配置存在冲突')}
          </p>
          {validation.blocked && validation.blocked.length > 0 && (
            <p className="text-xs mt-1">
              {t('agent.tools.blocked_hint', {
                names: validation.blocked.map(id => ontologies.find(o => o.id === id)?.name ?? id).join(', '),
              })}
            </p>
          )}
          <button type="button" onClick={() => setDrawerOpen(true)}
            className="mt-2 px-3 py-1 text-xs border border-current rounded hover:opacity-80">
            {t('agent.tools.view_capabilities', '查看能力交集')}
          </button>
        </div>
      )}

      {canEdit && (
        <button type="button" disabled={saving || !dirty} onClick={save}
          className="bg-black text-white rounded-lg px-4 py-2 text-sm font-medium hover:bg-gray-800 disabled:opacity-40">
          {saving ? t('agent.tools.saving', '保存中…') : t('agent.tools.save', '保存')}
        </button>
      )}

      <ExternalToolCard bindings={externalBindings} canEdit={canEdit}
        onBind={bindExternal} onUnbind={unbindExternal} bindError={externalError}
        skillBindings={skillBindings} onBindSkill={bindSkillFn} onUnbindSkill={unbindSkillFn}
        skillBindError={skillBindError} />

      <p className="text-xs text-gray-400">
        {t('agent.tools.next_turn_refresh', '工具选择将在下一次 Turn 生效并刷新')}
      </p>

      <CapabilityDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)}
        capabilities={validation?.capabilities ?? []} />
    </div>
  )
}
