import '@/i18n'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { apiClientV2 } from '@/api/client'
import ConnectionsTab from './ConnectionsTab'
import { useAuthStore } from '@/stores/authStore'

vi.mock('@/api/client', () => ({
  apiClientV2: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))

describe('ConnectionsTab database connections', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useAuthStore.setState({ user: { id: 'editor', username: 'editor', email: '', role: 'editor', is_active: true, created_at: '' } })
  })
  afterEach(() => useAuthStore.setState({ user: null, token: null }))

  it('lets viewers read connections without exposing editor-only actions', async () => {
    useAuthStore.setState({ user: { ...useAuthStore.getState().user!, role: 'viewer' } })
    vi.mocked(apiClientV2.get).mockResolvedValueOnce([{ id: 'conn-1', name: 'Read-only connection', kind: 'mysql', status: 'active' }])
    render(<ConnectionsTab />)
    await screen.findByText('Read-only connection')
    expect(screen.queryByRole('button', { name: /新建连接|new connection/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /发现数据表|discover tables/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /删除|delete/i })).toBeNull()
  })

  it('tests a database config before saving it', async () => {
    vi.mocked(apiClientV2.get).mockResolvedValueOnce([])
    vi.mocked(apiClientV2.post)
      .mockResolvedValueOnce({ success: true })
      .mockResolvedValueOnce({ id: 'conn-1' })
      .mockResolvedValueOnce([])
    render(<ConnectionsTab />)

    await waitFor(() => expect(screen.getByRole('button', { name: /新建连接|new connection/i })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /新建连接|new connection/i }))
    fireEvent.change(screen.getByLabelText(/连接名称|connection name/i), { target: { value: 'Runtime MySQL' } })
    fireEvent.click(screen.getByRole('button', { name: /测试连接|test connection/i }))

    await waitFor(() => expect(screen.getByText(/连接成功|connected/i)).toBeTruthy())
    expect(apiClientV2.post).toHaveBeenCalledWith('/connections/test-config', expect.objectContaining({ type: 'mysql' }))
  })

  it('discovers tables for a saved database connection', async () => {
    vi.mocked(apiClientV2.get)
      .mockResolvedValueOnce([{ id: 'conn-1', name: 'Runtime MySQL', kind: 'mysql', status: 'active' }])
      .mockResolvedValueOnce({ resources: ['codex_pipeline_probe'] })
    render(<ConnectionsTab />)

    await waitFor(() => expect(screen.getByText('Runtime MySQL')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /发现数据表|discover tables/i }))
    await waitFor(() => expect(screen.getByText('codex_pipeline_probe')).toBeTruthy())
    expect(apiClientV2.get).toHaveBeenCalledWith('/connections/conn-1/resources')
    expect(screen.queryByRole('button', { name: /同步|sync/i })).toBeNull()
    expect(screen.getByText(/Pipeline Builder/)).toBeTruthy()
  })

  it('shows a resource discovery error instead of an empty table result', async () => {
    vi.mocked(apiClientV2.get)
      .mockResolvedValueOnce([{ id: 'conn-1', name: 'Runtime MySQL', kind: 'mysql', status: 'active' }])
      .mockRejectedValueOnce(new Error('database unavailable'))
    render(<ConnectionsTab />)

    await waitFor(() => expect(screen.getByText('Runtime MySQL')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /发现数据表|discover tables/i }))
    await waitFor(() => expect(screen.getByText('database unavailable')).toBeTruthy())
  })
})
