import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { apiClientV2 } from '@/api/client'

type DefinitionKind = 'logic' | 'actions'

interface Definition {
  id: string
  name: string
  description?: string
  logic_type?: string
  action_category?: string
  expression?: Record<string, unknown>
  effects?: unknown[]
  parameters?: unknown[]
  backed_by_function?: string | null
}

interface Props {
  ontologyId: string
  kind: DefinitionKind
}

export default function V2DefinitionEditor({ ontologyId, kind }: Props) {
  return <DefinitionEditorContent key={`${ontologyId}:${kind}`} ontologyId={ontologyId} kind={kind} />
}

function DefinitionEditorContent({ ontologyId, kind }: Props) {
  const { t } = useTranslation()
  const [items, setItems] = useState<Definition[]>([])
  const [selected, setSelected] = useState<Definition | null>(null)
  const [definition, setDefinition] = useState('{}')
  const [functionRef, setFunctionRef] = useState('')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  const load = async () => {
    try {
      const rows = await apiClientV2.get<Definition[]>(`/ontologies/${ontologyId}/${kind}`)
      setItems(Array.isArray(rows) ? rows : [])
    } catch {
      setError(t('v2def.load_failed'))
    }
  }

  useEffect(() => {
    let cancelled = false
    void apiClientV2.get<Definition[]>(`/ontologies/${ontologyId}/${kind}`)
      .then(rows => { if (!cancelled) setItems(Array.isArray(rows) ? rows : []) })
      .catch(() => { if (!cancelled) setError(t('v2def.load_failed')) })
    return () => { cancelled = true }
  }, [ontologyId, kind, t])

  const select = (item: Definition) => {
    setSelected(item)
    setDefinition(JSON.stringify(kind === 'logic' ? item.expression ?? {} : item.effects ?? [], null, 2))
    setFunctionRef(item.backed_by_function ?? '')
    setError('')
  }

  const save = async () => {
    if (!selected) return
    let parsed: Record<string, unknown> | unknown[]
    try {
      parsed = JSON.parse(definition) as Record<string, unknown> | unknown[]
    } catch {
      setError(t('v2def.invalid_json'))
      return
    }
    if ((kind === 'logic' && (Array.isArray(parsed) || parsed === null)) || (kind === 'actions' && !Array.isArray(parsed))) {
      setError(kind === 'logic' ? t('v2def.logic_must_be_object') : t('v2def.actions_must_be_array'))
      return
    }
    setSaving(true)
    setError('')
    try {
      const body = kind === 'logic'
        ? { expression: parsed }
        : { effects: parsed, backed_by_function: functionRef || null }
      await apiClientV2.patch(`/ontologies/${ontologyId}/${kind}/${selected.id}`, body)
      await load()
    } catch {
      setError(t('v2def.save_failed'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="bg-white border rounded-lg p-4 space-y-3" data-testid={`v2-${kind}-editor`}>
      <div>
        <h3 className="font-medium text-sm">{kind === 'logic' ? t('v2def.label_logic') : t('v2def.label_actions')}</h3>
        <p className="text-xs text-gray-500 mt-1">{t('v2def.unpublished_hint')}</p>
      </div>
      {items.length === 0 ? <p className="text-sm text-gray-400">{t('v2def.empty')}</p> : (
        <div className="flex flex-wrap gap-2">
          {items.map(item => <button key={item.id} type="button" onClick={() => select(item)}
            className="px-2 py-1 border rounded text-xs hover:bg-gray-50">{item.name}</button>)}
        </div>
      )}
      {selected && <>
        <textarea data-testid={`v2-${kind}-definition`} value={definition} onChange={event => setDefinition(event.target.value)}
          rows={8} className="w-full border rounded p-2 font-mono text-xs" />
        {kind === 'actions' && <input data-testid="v2-actions-function-ref" value={functionRef}
          onChange={event => setFunctionRef(event.target.value)} placeholder={t('v2def.ph_function_ref')}
          className="w-full border rounded px-2 py-1.5 text-sm" />}
        <button data-testid={`v2-${kind}-save`} type="button" disabled={saving} onClick={() => void save()}
          className="px-3 py-1.5 bg-black text-white rounded text-xs disabled:opacity-50">{t('v2def.save')}</button>
      </>}
      {error && <p role="alert" className="text-xs text-red-600">{error}</p>}
    </section>
  )
}
