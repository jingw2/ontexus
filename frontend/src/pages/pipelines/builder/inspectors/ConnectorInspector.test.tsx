import '@/i18n'
import { describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { apiClientV2 } from '@/api/client'
import ConnectorInspector from './ConnectorInspector'

vi.mock('@/api/client', () => ({
  apiClientV2: { post: vi.fn() },
}))

describe('ConnectorInspector test-connection button', () => {
  const dbConfig = { source_type: 'postgresql', config_values: { host: 'localhost', port: '5432' } }

  it('shows failed when the backend reports success: false (e.g. wrong credentials) instead of treating any 200 response as success', async () => {
    vi.mocked(apiClientV2.post).mockResolvedValueOnce({ success: false, detail: 'connection refused' })
    render(<ConnectorInspector config={dbConfig} onChange={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: /测试连接|Test Connection/i }))

    await waitFor(() => {
      expect(screen.getByText('connection refused')).toBeTruthy()
    })
  })

  it('shows success only when the backend actually reports success: true', async () => {
    vi.mocked(apiClientV2.post).mockResolvedValueOnce({ success: true })
    render(<ConnectorInspector config={dbConfig} onChange={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: /测试连接|Test Connection/i }))

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /连接成功|success/i })).toBeTruthy()
    })
  })
})
