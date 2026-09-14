import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { describe, it, expect, beforeAll, afterEach, afterAll, vi } from 'vitest'
import ToolConnectionsPage from './ToolConnectionsPage'
import type { ToolConnection, ToolConnectionVersion } from '@/api/toolConnections'
import type { SkillVersion } from '@/api/skills'

const server = setupServer(
  http.get('*/api/v2/skills/packages', () => HttpResponse.json({ data: { items: [] }, message: 'ok' })),
)
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ToolConnectionsPage />
    </QueryClientProvider>,
  )
}

const PROVIDER = { id: 'p-1', name: 'Web Search', kind: 'search', status: 'active' }

describe('ToolConnectionsPage', () => {
  it('shows an empty state when there are no providers', async () => {
    server.use(http.get('*/api/v2/tool-providers', () =>
      HttpResponse.json({ data: { items: [] }, message: 'ok' })))
    renderPage()
    expect(await screen.findByTestId('tool-providers-empty')).toBeTruthy()
  })

  it('lists a provider, expands it, and lists its connections', async () => {
    server.use(
      http.get('*/api/v2/tool-providers', () =>
        HttpResponse.json({ data: { items: [PROVIDER] }, message: 'ok' })),
      http.get('*/api/v2/tool-connections', () =>
        HttpResponse.json({ data: { items: [{ id: 'c-1', provider_id: 'p-1', status: 'active', active_version_id: null }] }, message: 'ok' })),
    )
    renderPage()
    expect(await screen.findByTestId('provider-card-p-1')).toBeTruthy()
    await userEvent.click(screen.getByTestId('provider-card-p-1').querySelector('button')!)
    expect(await screen.findByTestId('connection-row-c-1')).toBeTruthy()
  })

  it('creates a new provider via the modal', async () => {
    let created = false
    server.use(
      http.get('*/api/v2/tool-providers', () =>
        HttpResponse.json({ data: { items: created ? [PROVIDER] : [] }, message: 'ok' })),
      http.post('*/api/v2/tool-providers', () => {
        created = true
        return HttpResponse.json({ data: PROVIDER, message: 'ok' }, { status: 201 })
      }),
    )
    renderPage()
    await screen.findByTestId('tool-providers-empty')
    await userEvent.click(screen.getByTestId('create-provider-button'))
    await userEvent.type(screen.getByTestId('provider-name-input'), 'Web Search')
    await userEvent.click(screen.getByTestId('submit-create-provider'))
    await waitFor(() => expect(screen.getByTestId('provider-card-p-1')).toBeTruthy())
  })

  it('approves a pending version via the confirmation dialog, then activates it', async () => {
    const connection: ToolConnection = { id: 'c-1', provider_id: 'p-1', status: 'active', active_version_id: null }
    let versions: ToolConnectionVersion[] = [
      { id: 'v-1', connection_id: 'c-1', version_no: 1, endpoint: 'https://search.example.com', audience: null,
        scopes: ['search:read'], allowlists: {}, search_provider: null, approval_status: 'pending' as const,
        health_status: 'unknown' as const, created_by: 'u-1', created_at: '2026-01-01T00:00:00Z' },
    ]
    server.use(
      http.get('*/api/v2/tool-providers', () => HttpResponse.json({ data: { items: [PROVIDER] }, message: 'ok' })),
      http.get('*/api/v2/tool-connections', () => HttpResponse.json({ data: { items: [connection] }, message: 'ok' })),
      http.get('*/api/v2/tool-connections/c-1/versions', () => HttpResponse.json({ data: { items: versions }, message: 'ok' })),
      http.post('*/api/v2/tool-connections/versions/v-1/approve', () => {
        versions = versions.map(v => v.id === 'v-1' ? { ...v, approval_status: 'approved' as const } : v)
        return HttpResponse.json({ data: { id: 'v-1', approval_status: 'approved' }, message: 'ok' })
      }),
      http.post('*/api/v2/tool-connections/activate', () => {
        connection.active_version_id = 'v-1'
        return HttpResponse.json({ data: { connection_id: 'c-1', active_version_id: 'v-1' }, message: 'ok' })
      }),
    )
    renderPage()
    await userEvent.click((await screen.findByTestId('provider-card-p-1')).querySelector('button')!)
    await userEvent.click((await screen.findByTestId('connection-row-c-1')).querySelector('button')!)
    expect(await screen.findByTestId('version-row-v-1')).toBeTruthy()

    await userEvent.click(screen.getByTestId('approve-version-v-1'))
    expect(await screen.findByTestId('approve-version-dialog')).toBeTruthy()
    await userEvent.click(screen.getByTestId('confirm-approve-version'))
    await waitFor(() => expect(screen.getByTestId('activate-version-v-1')).toBeTruthy())

    await userEvent.click(screen.getByTestId('activate-version-v-1'))
    await waitFor(() => expect(screen.queryByTestId('activate-version-v-1')).toBeNull())
  })

  it('lists a skill package, approves a pending version via the confirmation dialog', async () => {
    const pkg = { id: 'sk-1', name: 'PDF Extractor', status: 'active' }
    let versions: SkillVersion[] = [
      { id: 'sv-1', package_id: 'sk-1', version_no: 1, approval_status: 'pending' as const, canonical_hash: 'deadbeef', manifest: {} },
    ]
    server.use(
      http.get('*/api/v2/tool-providers', () => HttpResponse.json({ data: { items: [] }, message: 'ok' })),
      http.get('*/api/v2/skills/packages', () => HttpResponse.json({ data: { items: [pkg] }, message: 'ok' })),
      http.get('*/api/v2/skills/versions', () => HttpResponse.json({ data: { items: versions }, message: 'ok' })),
      http.post('*/api/v2/skills/versions/sv-1/approve', () => {
        versions = versions.map(v => v.id === 'sv-1' ? { ...v, approval_status: 'approved' as const } : v)
        return HttpResponse.json({ data: { id: 'sv-1', approval_status: 'approved' }, message: 'ok' })
      }),
    )
    renderPage()
    expect(await screen.findByTestId('skill-package-card-sk-1')).toBeTruthy()
    await userEvent.click(screen.getByTestId('skill-package-card-sk-1').querySelector('button')!)
    expect(await screen.findByTestId('skill-version-row-sv-1')).toBeTruthy()

    await userEvent.click(screen.getByTestId('approve-skill-version-sv-1'))
    expect(await screen.findByTestId('approve-skill-version-dialog')).toBeTruthy()
    await userEvent.click(screen.getByTestId('confirm-approve-skill-version'))
    await waitFor(() => expect(screen.queryByTestId('approve-skill-version-sv-1')).toBeNull())
  })

  it('renames a skill package and refreshes the list', async () => {
    let pkg = { id: 'sk-1', name: 'PDF Extractor', status: 'active' }
    let renameBody: Record<string, unknown> | null = null
    server.use(
      http.get('*/api/v2/tool-providers', () => HttpResponse.json({ data: { items: [] }, message: 'ok' })),
      http.get('*/api/v2/skills/packages', () => HttpResponse.json({ data: { items: [pkg] }, message: 'ok' })),
      http.put('*/api/v2/skills/packages/sk-1', async ({ request }) => {
        renameBody = await request.json() as Record<string, unknown>
        pkg = { ...pkg, name: renameBody.name as string }
        return HttpResponse.json({ data: { id: 'sk-1', name: renameBody.name }, message: 'ok' })
      }),
    )
    renderPage()
    await screen.findByTestId('skill-package-card-sk-1')
    await userEvent.click(screen.getByTestId('rename-skill-package-sk-1'))
    const input = screen.getByTestId('rename-skill-package-input-sk-1')
    await userEvent.clear(input)
    await userEvent.type(input, '客户支持技能包')
    await userEvent.click(screen.getByTestId('save-rename-skill-package-sk-1'))
    await waitFor(() => expect(renameBody).toMatchObject({ name: '客户支持技能包' }))
    await waitFor(() => expect(screen.queryByTestId('rename-skill-package-input-sk-1')).toBeNull())
  })

  it('uploads a skill file, scans it, and shows the auto-approved result', async () => {
    const pkg = { id: 'sk-2', name: 'Uploaded Skills', status: 'active' }
    let uploadCalled = false
    server.use(
      http.get('*/api/v2/tool-providers', () => HttpResponse.json({ data: { items: [] }, message: 'ok' })),
      http.get('*/api/v2/skills/packages', () => HttpResponse.json({ data: { items: [pkg] }, message: 'ok' })),
      http.get('*/api/v2/skills/versions', () => HttpResponse.json({ data: { items: [] }, message: 'ok' })),
      http.post('*/api/v2/skills/versions/upload', async () => {
        uploadCalled = true
        return HttpResponse.json({
          data: { id: 'sv-new', package_id: 'sk-2', version_no: 1, canonical_hash: 'abc',
                  approval_status: 'approved', scan_report: [] },
          message: 'ok',
        }, { status: 201 })
      }),
    )
    renderPage()
    expect(await screen.findByTestId('skill-package-card-sk-2')).toBeTruthy()
    await userEvent.click(screen.getByTestId('skill-package-card-sk-2').querySelector('button')!)
    await userEvent.click(screen.getByTestId('upload-skill-version-sk-2'))

    const file = new File(['---\nname: x\ndescription: y\n---\nbody'], 'SKILL.md', { type: 'text/markdown' })
    const fileInput = screen.getByTestId('skill-upload-file-input') as HTMLInputElement
    await userEvent.upload(fileInput, file)
    await userEvent.click(screen.getByTestId('submit-upload-skill-version'))

    await waitFor(() => expect(screen.getByTestId('skill-upload-result-sk-2')).toBeTruthy())
    expect(uploadCalled).toBe(true)
  })

  it('creates a connection version via the create-version form', async () => {
    const connection = { id: 'c-2', provider_id: 'p-1', status: 'active', active_version_id: null }
    server.use(
      http.get('*/api/v2/tool-providers', () => HttpResponse.json({ data: { items: [PROVIDER] }, message: 'ok' })),
      http.get('*/api/v2/tool-connections', () => HttpResponse.json({ data: { items: [connection] }, message: 'ok' })),
      http.get('*/api/v2/tool-connections/c-2/versions', () => HttpResponse.json({ data: { items: [] }, message: 'ok' })),
      http.post('*/api/v2/tool-connections/versions', async ({ request }) => {
        const body = await request.json() as Record<string, unknown>
        expect(body.connection_id).toBe('c-2')
        expect(body.endpoint).toBe('https://new.example.com')
        return HttpResponse.json({ data: { id: 'v-new', connection_id: 'c-2', version_no: 1, approval_status: 'pending' }, message: 'ok' }, { status: 201 })
      }),
    )
    renderPage()
    await userEvent.click((await screen.findByTestId('provider-card-p-1')).querySelector('button')!)
    await userEvent.click((await screen.findByTestId('connection-row-c-2')).querySelector('button')!)
    await userEvent.click(screen.getByTestId('create-version-c-2'))
    await userEvent.type(screen.getByTestId('version-endpoint-input'), 'https://new.example.com')
    await userEvent.click(screen.getByTestId('submit-create-version'))
    await waitFor(() => expect(screen.queryByTestId('submit-create-version')).toBeNull())
  })

  it('create-version form only shows fields relevant to the provider kind', async () => {
    const connection = { id: 'c-2', provider_id: 'p-1', status: 'active', active_version_id: null }
    server.use(
      http.get('*/api/v2/tool-providers', () => HttpResponse.json({ data: { items: [PROVIDER] }, message: 'ok' })),
      http.get('*/api/v2/tool-connections', () => HttpResponse.json({ data: { items: [connection] }, message: 'ok' })),
      http.get('*/api/v2/tool-connections/c-2/versions', () => HttpResponse.json({ data: { items: [] }, message: 'ok' })),
    )
    renderPage()
    await userEvent.click((await screen.findByTestId('provider-card-p-1')).querySelector('button')!)
    await userEvent.click((await screen.findByTestId('connection-row-c-2')).querySelector('button')!)
    await userEvent.click(screen.getByTestId('create-version-c-2'))
    // PROVIDER is kind 'search': endpoint + API-key credential shown, OAuth/domain fields hidden
    // (no i18next instance in this test file, so t() renders the raw key)
    expect(screen.getByTestId('version-endpoint-input')).toBeTruthy()
    expect(screen.getByPlaceholderText('toolConnections.api_key')).toBeTruthy()
    expect(screen.queryByPlaceholderText('toolConnections.audience')).toBeNull()
    expect(screen.queryByPlaceholderText('toolConnections.scopes')).toBeNull()
    expect(screen.queryByPlaceholderText('toolConnections.allowlist_domains')).toBeNull()
  })

  it('create-version form hides endpoint/credential and shows only domains for playwright', async () => {
    const pwProvider = { id: 'p-pw', name: 'Playwright', kind: 'playwright', status: 'active' }
    const connection = { id: 'c-pw', provider_id: 'p-pw', status: 'active', active_version_id: null }
    server.use(
      http.get('*/api/v2/tool-providers', () => HttpResponse.json({ data: { items: [pwProvider] }, message: 'ok' })),
      http.get('*/api/v2/tool-connections', () => HttpResponse.json({ data: { items: [connection] }, message: 'ok' })),
      http.get('*/api/v2/tool-connections/c-pw/versions', () => HttpResponse.json({ data: { items: [] }, message: 'ok' })),
    )
    renderPage()
    await userEvent.click((await screen.findByTestId('provider-card-p-pw')).querySelector('button')!)
    await userEvent.click((await screen.findByTestId('connection-row-c-pw')).querySelector('button')!)
    await userEvent.click(screen.getByTestId('create-version-c-pw'))
    expect(screen.queryByTestId('version-endpoint-input')).toBeNull()
    expect(screen.queryByPlaceholderText('toolConnections.credential_reference')).toBeNull()
    expect(screen.queryByPlaceholderText('toolConnections.api_key')).toBeNull()
    expect(screen.getByPlaceholderText('toolConnections.allowlist_domains')).toBeTruthy()
  })

  it('selecting a search provider preset auto-fills the endpoint field', async () => {
    const connection = { id: 'c-2', provider_id: 'p-1', status: 'active', active_version_id: null }
    server.use(
      http.get('*/api/v2/tool-providers', () => HttpResponse.json({ data: { items: [PROVIDER] }, message: 'ok' })),
      http.get('*/api/v2/tool-connections', () => HttpResponse.json({ data: { items: [connection] }, message: 'ok' })),
      http.get('*/api/v2/tool-connections/c-2/versions', () => HttpResponse.json({ data: { items: [] }, message: 'ok' })),
    )
    renderPage()
    await userEvent.click((await screen.findByTestId('provider-card-p-1')).querySelector('button')!)
    await userEvent.click((await screen.findByTestId('connection-row-c-2')).querySelector('button')!)
    await userEvent.click(screen.getByTestId('create-version-c-2'))

    expect((screen.getByTestId('version-endpoint-input') as HTMLInputElement).value).toBe('')
    await userEvent.selectOptions(screen.getByTestId('version-search-provider-select'), 'bing')
    expect((screen.getByTestId('version-endpoint-input') as HTMLInputElement).value)
      .toBe('https://api.bing.microsoft.com/v7.0/search')
    await userEvent.selectOptions(screen.getByTestId('version-search-provider-select'), 'brave')
    expect((screen.getByTestId('version-endpoint-input') as HTMLInputElement).value)
      .toBe('https://api.search.brave.com/res/v1/web/search')
  })

  it('edits a pending version via the edit form and refreshes the version list', async () => {
    const connection: ToolConnection = { id: 'c-1', provider_id: 'p-1', status: 'active', active_version_id: null }
    let versions: ToolConnectionVersion[] = [
      { id: 'v-1', connection_id: 'c-1', version_no: 1, endpoint: 'https://search.example.com', audience: null,
        scopes: [], allowlists: {}, search_provider: 'generic', approval_status: 'pending' as const,
        health_status: 'unknown' as const, created_by: 'u-1', created_at: '2026-01-01T00:00:00Z' },
    ]
    server.use(
      http.get('*/api/v2/tool-providers', () => HttpResponse.json({ data: { items: [PROVIDER] }, message: 'ok' })),
      http.get('*/api/v2/tool-connections', () => HttpResponse.json({ data: { items: [connection] }, message: 'ok' })),
      http.get('*/api/v2/tool-connections/c-1/versions', () => HttpResponse.json({ data: { items: versions }, message: 'ok' })),
      http.put('*/api/v2/tool-connections/versions/v-1', async ({ request }) => {
        const body = await request.json() as Record<string, unknown>
        expect(body.endpoint).toBe('https://updated.example.com')
        expect(body.credential_reference).toBeUndefined()
        versions = versions.map(v => v.id === 'v-1' ? { ...v, endpoint: 'https://updated.example.com' } : v)
        return HttpResponse.json({ data: { id: 'v-1', approval_status: 'pending' }, message: 'ok' })
      }),
    )
    renderPage()
    await userEvent.click((await screen.findByTestId('provider-card-p-1')).querySelector('button')!)
    await userEvent.click((await screen.findByTestId('connection-row-c-1')).querySelector('button')!)
    expect(await screen.findByTestId('version-row-v-1')).toBeTruthy()

    await userEvent.click(screen.getByTestId('edit-version-v-1'))
    const endpointInput = await screen.findByTestId('version-endpoint-input')
    expect((endpointInput as HTMLInputElement).value).toBe('https://search.example.com')
    await userEvent.clear(endpointInput)
    await userEvent.type(endpointInput, 'https://updated.example.com')
    await userEvent.click(screen.getByTestId('submit-edit-version-v-1'))

    await waitFor(() => expect(screen.queryByTestId('edit-version-form-v-1')).toBeNull())
  })

  it('shows the active version\'s configured search provider instead of a raw id', async () => {
    const connection: ToolConnection = {
      id: 'c-1', provider_id: 'p-1', status: 'active', active_version_id: 'v-1',
      active_version_endpoint: 'https://api.bing.microsoft.com/v7.0/search', active_version_search_provider: 'bing',
    }
    server.use(
      http.get('*/api/v2/tool-providers', () => HttpResponse.json({ data: { items: [PROVIDER] }, message: 'ok' })),
      http.get('*/api/v2/tool-connections', () => HttpResponse.json({ data: { items: [connection] }, message: 'ok' })),
    )
    renderPage()
    await userEvent.click((await screen.findByTestId('provider-card-p-1')).querySelector('button')!)
    const row = await screen.findByTestId('connection-row-c-1')
    // no i18next instance in this test file, so t() renders the raw key — the preset's fallback label shows through
    expect(row.textContent).toContain('Bing Web Search API v7')
  })

  it('prefers the connection\'s custom name over the derived label', async () => {
    const connection: ToolConnection = {
      id: 'c-1', provider_id: 'p-1', status: 'active', active_version_id: 'v-1',
      name: '生产环境-Bing', active_version_endpoint: 'https://api.bing.microsoft.com/v7.0/search',
      active_version_search_provider: 'bing',
    }
    server.use(
      http.get('*/api/v2/tool-providers', () => HttpResponse.json({ data: { items: [PROVIDER] }, message: 'ok' })),
      http.get('*/api/v2/tool-connections', () => HttpResponse.json({ data: { items: [connection] }, message: 'ok' })),
    )
    renderPage()
    await userEvent.click((await screen.findByTestId('provider-card-p-1')).querySelector('button')!)
    const row = await screen.findByTestId('connection-row-c-1')
    expect(row.textContent).toContain('生产环境-Bing')
    expect(row.textContent).not.toContain('Bing Web Search API v7')
  })

  it('renames a connection and refreshes the list', async () => {
    let connection: ToolConnection = { id: 'c-1', provider_id: 'p-1', status: 'active', active_version_id: null }
    let renameBody: Record<string, unknown> | null = null
    server.use(
      http.get('*/api/v2/tool-providers', () => HttpResponse.json({ data: { items: [PROVIDER] }, message: 'ok' })),
      http.get('*/api/v2/tool-connections', () => HttpResponse.json({ data: { items: [connection] }, message: 'ok' })),
      http.put('*/api/v2/tool-connections/c-1', async ({ request }) => {
        renameBody = await request.json() as Record<string, unknown>
        connection = { ...connection, name: renameBody.name as string }
        return HttpResponse.json({ data: { id: 'c-1', name: renameBody.name }, message: 'ok' })
      }),
    )
    renderPage()
    await userEvent.click((await screen.findByTestId('provider-card-p-1')).querySelector('button')!)
    await screen.findByTestId('connection-row-c-1')
    await userEvent.click(screen.getByTestId('rename-connection-c-1'))
    await userEvent.type(screen.getByTestId('rename-connection-input-c-1'), '生产环境-Bing')
    await userEvent.click(screen.getByTestId('save-rename-connection-c-1'))
    await waitFor(() => expect(renameBody).toMatchObject({ name: '生产环境-Bing' }))
    await waitFor(() => expect(screen.queryByTestId('rename-connection-input-c-1')).toBeNull())
  })

  it('deletes a pending version after confirmation and refreshes the list', async () => {
    const connection: ToolConnection = { id: 'c-1', provider_id: 'p-1', status: 'active', active_version_id: null }
    let versions: ToolConnectionVersion[] = [
      { id: 'v-1', connection_id: 'c-1', version_no: 1, endpoint: 'https://search.example.com', audience: null,
        scopes: [], allowlists: {}, search_provider: 'generic', approval_status: 'pending' as const,
        health_status: 'unknown' as const, created_by: 'u-1', created_at: '2026-01-01T00:00:00Z' },
    ]
    let deleteCalled = false
    server.use(
      http.get('*/api/v2/tool-providers', () => HttpResponse.json({ data: { items: [PROVIDER] }, message: 'ok' })),
      http.get('*/api/v2/tool-connections', () => HttpResponse.json({ data: { items: [connection] }, message: 'ok' })),
      http.get('*/api/v2/tool-connections/c-1/versions', () => HttpResponse.json({ data: { items: versions }, message: 'ok' })),
      http.delete('*/api/v2/tool-connections/versions/v-1', () => {
        deleteCalled = true
        versions = []
        return HttpResponse.json({ data: { id: 'v-1', deleted: true }, message: 'ok' })
      }),
    )
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderPage()
    await userEvent.click((await screen.findByTestId('provider-card-p-1')).querySelector('button')!)
    await userEvent.click((await screen.findByTestId('connection-row-c-1')).querySelector('button')!)
    expect(await screen.findByTestId('version-row-v-1')).toBeTruthy()

    await userEvent.click(screen.getByTestId('delete-version-v-1'))
    await waitFor(() => expect(screen.queryByTestId('version-row-v-1')).toBeNull())
    expect(deleteCalled).toBe(true)
    confirmSpy.mockRestore()
  })

  it('does not delete a version when the confirmation is dismissed', async () => {
    const connection: ToolConnection = { id: 'c-1', provider_id: 'p-1', status: 'active', active_version_id: null }
    const versions: ToolConnectionVersion[] = [
      { id: 'v-1', connection_id: 'c-1', version_no: 1, endpoint: 'https://search.example.com', audience: null,
        scopes: [], allowlists: {}, search_provider: 'generic', approval_status: 'pending' as const,
        health_status: 'unknown' as const, created_by: 'u-1', created_at: '2026-01-01T00:00:00Z' },
    ]
    let deleteCalled = false
    server.use(
      http.get('*/api/v2/tool-providers', () => HttpResponse.json({ data: { items: [PROVIDER] }, message: 'ok' })),
      http.get('*/api/v2/tool-connections', () => HttpResponse.json({ data: { items: [connection] }, message: 'ok' })),
      http.get('*/api/v2/tool-connections/c-1/versions', () => HttpResponse.json({ data: { items: versions }, message: 'ok' })),
      http.delete('*/api/v2/tool-connections/versions/v-1', () => {
        deleteCalled = true
        return HttpResponse.json({ data: { id: 'v-1', deleted: true }, message: 'ok' })
      }),
    )
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderPage()
    await userEvent.click((await screen.findByTestId('provider-card-p-1')).querySelector('button')!)
    await userEvent.click((await screen.findByTestId('connection-row-c-1')).querySelector('button')!)
    expect(await screen.findByTestId('version-row-v-1')).toBeTruthy()

    await userEvent.click(screen.getByTestId('delete-version-v-1'))
    expect(deleteCalled).toBe(false)
    expect(screen.getByTestId('version-row-v-1')).toBeTruthy()
    confirmSpy.mockRestore()
  })

  it('issue-token form clears and only one is open when switching between versions', async () => {
    const mcpProvider = { id: 'p-mcp', name: 'MCP Provider', kind: 'external_mcp', status: 'active' }
    const connection = { id: 'c-3', provider_id: 'p-mcp', status: 'active', active_version_id: null }
    const versions = [
      { id: 'v-a', connection_id: 'c-3', version_no: 1, endpoint: null, audience: null, scopes: [], allowlists: {}, approval_status: 'approved' as const, health_status: 'unknown' as const, created_by: 'u-1', created_at: '2026-01-01T00:00:00Z' },
      { id: 'v-b', connection_id: 'c-3', version_no: 2, endpoint: null, audience: null, scopes: [], allowlists: {}, approval_status: 'approved' as const, health_status: 'unknown' as const, created_by: 'u-1', created_at: '2026-01-01T00:00:00Z' },
    ]
    server.use(
      http.get('*/api/v2/tool-providers', () => HttpResponse.json({ data: { items: [mcpProvider] }, message: 'ok' })),
      http.get('*/api/v2/tool-connections', () => HttpResponse.json({ data: { items: [connection] }, message: 'ok' })),
      http.get('*/api/v2/tool-connections/c-3/versions', () => HttpResponse.json({ data: { items: versions }, message: 'ok' })),
    )
    renderPage()
    await userEvent.click((await screen.findByTestId('provider-card-p-mcp')).querySelector('button')!)
    await userEvent.click((await screen.findByTestId('connection-row-c-3')).querySelector('button')!)
    await screen.findByTestId('version-row-v-a')

    await userEvent.click(screen.getByTestId('issue-token-toggle-v-a'))
    await userEvent.type(screen.getByTestId('mcp-token-input-v-a'), 'TOKEN-FOR-A')
    await userEvent.click(screen.getByTestId('issue-token-toggle-v-b'))

    // opening v-b's form must close v-a's — only one open at a time
    expect(screen.queryByTestId('issue-token-form-v-a')).toBeNull()
    // v-b's form must start empty, not carry over what was typed for v-a
    expect(screen.getByTestId('mcp-token-input-v-b')).toHaveValue('')
  })
})
