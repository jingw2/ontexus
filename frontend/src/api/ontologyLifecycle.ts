import { apiClient } from './client'
import type { OntologyStatus } from '@/types/ontology'
import type { TFunction } from 'i18next'

export interface LifecycleReceipt {
  ontology_id: string
  status?: string
  runtime_disabled?: boolean
}

export interface OntologyReleaseSummary {
  id: string
  version_no: number
  version: string
  created_by?: string | null
  created_at?: string | null
}

export interface OntologyReleaseDetail extends OntologyReleaseSummary {
  ontology_id: string
  schema_hash: string
  manifest_projection?: Record<string, unknown> | null
}

export interface PublishReceipt {
  ontology_id?: string
  release?: { version_no: number; version: string } | null
  schema_hash?: string
  entities?: unknown[]
  relations?: unknown[]
  [key: string]: unknown
}

let idempotencyCounter = 0

/** P1C-API idempotency key: ^[\x21-\x7e]{16,128}$ printable ASCII. */
export function newIdempotencyKey(): string {
  idempotencyCounter += 1
  return `pub-${Date.now().toString(36)}-${idempotencyCounter.toString(36).padStart(9, '0')}`
}

export const ontologyLifecycleApi = {
  markCreated: (ontologyId: string) =>
    apiClient.post<LifecycleReceipt>(
      `/ontologies/${ontologyId}/mark-created`,
      {},
      { headers: { 'Idempotency-Key': newIdempotencyKey() } },
    ),
  publish: (ontologyId: string, body: { base_working_revision?: number; changelog?: string }) =>
    apiClient.post<PublishReceipt>(
      `/ontologies/${ontologyId}/publish`,
      body,
      { headers: { 'Idempotency-Key': newIdempotencyKey() } },
    ),
  archive: (ontologyId: string, reason?: string) =>
    apiClient.post<LifecycleReceipt>(`/ontologies/${ontologyId}/archive`, { reason }),
  runtimeDisable: (ontologyId: string, reason?: string) =>
    apiClient.post<LifecycleReceipt>(`/ontologies/${ontologyId}/runtime-disable`, { reason }),
  runtimeEnable: (ontologyId: string, reason?: string) =>
    apiClient.post<LifecycleReceipt>(`/ontologies/${ontologyId}/runtime-enable`, { reason }),
  listReleases: (ontologyId: string) =>
    apiClient.get<{ items: OntologyReleaseSummary[]; next_cursor: string | null; has_more: boolean }>(
      `/ontologies/${ontologyId}/releases`,
    ),
  getRelease: (ontologyId: string, releaseId: string) =>
    apiClient.get<OntologyReleaseDetail>(`/ontologies/${ontologyId}/releases/${releaseId}`),
}

export function displayStatus(status: OntologyStatus | string | undefined, t: TFunction): string {
  switch (status) {
    case 'draft': return t('ontology.status_draft', 'Draft')
    case 'creating': return t('ontology.status_creating', 'Creating')
    case 'created': return t('ontology.status_created', 'Created')
    case 'published': return t('ontology.status_published', 'Published')
    case 'archived': return t('ontology.status_archived', 'Archived')
    default: return status ?? t('common.unknown', 'Unknown')
  }
}
