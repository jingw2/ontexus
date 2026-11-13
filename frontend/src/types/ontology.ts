export type OntologyStatus = 'draft' | 'creating' | 'created' | 'published' | 'archived'

export interface OntologyListItem {
  id: string
  name: string
  domain: string
  version: string
  status: OntologyStatus
  build_mode?: string
  entity_count: number
  relation_count: number
  created_by: string
  created_at: string
  updated_at: string
}

export interface OntologyDetail extends OntologyListItem {
  description?: string
  build_mode?: string
  is_dirty?: boolean
  created_at: string
}

export interface Entity {
  id: string
  ontology_id: string
  name_cn: string
  name_en?: string
  name_abbr?: string
  snomed_id?: string
  canonical_id?: string
  type?: string
  description?: string
  properties: Record<string, unknown>
  confidence: number
  version: string
  created_at: string
  updated_at: string
}

export type LogicFunctionType = 'derived_property' | 'aggregation' | 'complex_edit' | 'external_query'

export interface LogicRule {
  id: string
  ontology_id: string
  name_cn: string
  name_en?: string
  description?: string
  function_type?: LogicFunctionType | string
  definition?: string
  confidence: number
  version: string
  enabled?: boolean
  status?: string
  linked_entities: string[]
  created_at: string
  updated_at: string
}

export interface ActionParameter {
  name: string
  type: 'object_reference' | 'string' | 'number' | 'boolean' | 'enum' | 'date' | string
  description?: string
}

export interface ActionRule {
  operation: 'Modify' | 'Create Object' | 'Delete Object' | 'Create Link' | 'Delete Link' | string
  target: string
  value?: string
}

export interface ActionSideEffect {
  type: 'Notification' | 'Webhook' | string
  target: string
  detail?: string
}

export interface Action {
  id: string
  ontology_id: string
  name_cn: string
  name_en?: string
  description?: string
  parameters?: ActionParameter[]
  rules?: ActionRule[]
  submission_criteria?: string[]
  side_effects?: ActionSideEffect[]
  linked_entities: string[]
  linked_logic_ids: string[]
  confidence: number
  version: string
  status?: string
  enabled?: boolean
  created_at: string
  updated_at: string
}

export interface UploadedFile {
  id: string
  ontology_id: string
  filename: string
  file_size: number
  mime_type?: string
  created_at: string
}

export interface Prompt {
  id: string
  name: string
  domain: string
  content: string
  version: string
  created_by: string
  created_at: string
  updated_at: string
}

export interface ModelConfig {
  id: string
  name: string
  config_type: 'llm' | 'ocr' | 'other'
  provider: string
  api_base?: string
  models: string[]
  options?: Record<string, unknown>
  created_by: string
  created_at: string
  updated_at: string
}

export const DOMAINS = ['供应链','采购','财务','医疗','金融','法律','教育','科技','制造','能源','其他']
