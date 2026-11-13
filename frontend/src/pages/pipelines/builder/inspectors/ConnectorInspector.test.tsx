import '@/i18n'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { apiClientV2 } from '@/api/client'
import ConnectorInspector from './ConnectorInspector'

vi.mock('@/api/client', () => ({
  apiClientV2: { post: vi.fn(), get: vi.fn() },
}))

describe('ConnectorInspector test-connection button', () => {
  const dbConfig = { source_type: 'postgresql', connection_id: 'conn-1', config_values: { host: 'localhost', port: '5432' } }

  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(apiClientV2.get).mockResolvedValue([])
  })

  it('never displays tables from a previously selected connection', async () => {
    let finishSecond!: (value: { resources: string[] }) => void
    const second = new Promise<{ resources: string[] }>(resolve => { finishSecond = resolve })
    vi.mocked(apiClientV2.get).mockImplementation(url => {
      if (url === '/connections/a/resources') return Promise.resolve({ resources: ['a_table'] }) as never
      if (url === '/connections/b/resources') return second as never
      return Promise.resolve([]) as never
    })
    const view = render(<ConnectorInspector config={{ source_type: 'postgresql', connection_id: 'a' }} onChange={() => {}} />)
    await screen.findByRole('option', { name: 'a_table' })
    view.rerender(<ConnectorInspector config={{ source_type: 'postgresql', connection_id: 'b' }} onChange={() => {}} />)
    expect(screen.queryByRole('option', { name: 'a_table' })).toBeNull()
    expect(screen.getByLabelText(/源数据表|source table/i)).toBeDisabled()
    view.rerender(<ConnectorInspector config={{ source_type: 'postgresql', connection_id: '' }} onChange={() => {}} />)
    await act(async () => { finishSecond({ resources: ['b_table'] }); await second })
    expect(screen.queryByRole('option', { name: 'b_table' })).toBeNull()
    expect(screen.getByLabelText(/源数据表|source table/i)).toBeDisabled()
  })

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

  it('does not test SQL with inline config when no saved connection is selected', async () => {
    render(<ConnectorInspector config={{ source_type: 'postgresql', config_values: { host: 'localhost', port: '5432' } }} onChange={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: /测试连接|Test Connection/i }))

    await waitFor(() => expect(screen.getByRole('button', { name: /select a saved connection|选择已保存的连接/i })).toBeTruthy())
    expect(apiClientV2.post).not.toHaveBeenCalled()
  })

  it('keeps file sources as a local readiness check', async () => {
    render(<ConnectorInspector config={{ source_type: 'file', files: [{ name: 'orders.csv', size: 128 }] }} onChange={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: /测试连接|Test Connection/i }))

    await waitFor(() => expect(screen.getByRole('button', { name: /连接成功|success/i })).toBeTruthy())
    expect(apiClientV2.post).not.toHaveBeenCalledWith('/connections/test-config', expect.anything())
  })

  it('selects a saved database connection and one of its discovered tables', async () => {
    vi.mocked(apiClientV2.get).mockImplementation(async (url: string) => {
      if (url === '/connections') return [{ id: 'conn-1', name: 'Runtime PostgreSQL', kind: 'postgres', status: 'active' }]
      if (url === '/connections/conn-1/resources') return { resources: ['codex_pipeline_probe'] }
      return []
    })
    const onChange = vi.fn()
    function Harness() {
      const [config, setConfig] = useState<Record<string, unknown>>({ source_type: 'postgresql' })
      return <ConnectorInspector config={config} onChange={(key, value) => { onChange(key, value); setConfig(previous => ({ ...previous, [key]: value })) }} />
    }
    render(<Harness />)

    const savedConnection = await screen.findByLabelText(/已保存连接|saved connection/i)
    fireEvent.change(savedConnection, { target: { value: 'conn-1' } })
    const table = await screen.findByLabelText(/源数据表|source table/i)
    await screen.findByRole('option', { name: 'codex_pipeline_probe' })
    fireEvent.change(table, { target: { value: 'codex_pipeline_probe' } })

    expect(onChange).toHaveBeenCalledWith('connection_id', 'conn-1')
    expect(onChange).toHaveBeenCalledWith('resource', 'codex_pipeline_probe')
    expect(onChange).not.toHaveBeenCalledWith('config_values', expect.objectContaining({ password: expect.anything() }))
  })

  it('does not offer unsupported MongoDB or REST source types in Builder', () => {
    render(<ConnectorInspector config={{ source_type: 'file' }} onChange={() => {}} />)

    expect(screen.queryByRole('option', { name: /MongoDB/i })).toBeNull()
    expect(screen.queryByRole('option', { name: /REST API/i })).toBeNull()
  })

  it('renders legacy MongoDB and REST nodes as unsupported without exposing inline secrets', () => {
    const { rerender } = render(<ConnectorInspector
      config={{
        source_type: 'mongodb',
        config_values: { uri: 'mongodb://user:secret@example.invalid/orders' },
      }}
      onChange={() => {}}
      readOnly
    />)

    expect(screen.getByRole('alert')).toHaveTextContent(/unsupported in Builder/i)
    expect(screen.queryByText(/mongodb:\/\/user:secret/i)).toBeNull()
    expect(screen.queryByText(/secret/i)).toBeNull()
    expect(screen.queryByRole('textbox')).toBeNull()

    rerender(<ConnectorInspector
      config={{
        source_type: 'rest_api',
        config_values: {
          url: 'https://example.invalid/orders',
          headers: '{"Authorization":"Bearer secret"}',
        },
      }}
      onChange={() => {}}
      readOnly
    />)

    expect(screen.getByRole('alert')).toHaveTextContent(/unsupported in Builder/i)
    expect(screen.queryByText(/Authorization|Bearer secret|example\.invalid/i)).toBeNull()
    expect(screen.queryByRole('textbox')).toBeNull()
  })
})
