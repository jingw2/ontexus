import '@/i18n'
import { afterEach, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { apiClientV2 } from '@/api/client'
import { useAuthStore } from '@/stores/authStore'
import CuratedTab from './CuratedTab'

afterEach(() => { vi.restoreAllMocks(); useAuthStore.setState({ user: null, token: null }) })

it.each(['viewer', 'editor', 'admin'] as const)('shows review controls only to admin (current: %s)', async role => {
  useAuthStore.setState({ user: { id: role, username: role, email: '', role, is_active: true, created_at: '' } })
  vi.spyOn(apiClientV2, 'get').mockResolvedValue([{ id: 'curated-1', name: 'Review data', status: 'pending_review', kind: 'structured' }])
  render(<CuratedTab />)
  await screen.findByText('Review data')
  const approve = screen.queryByRole('button', { name: /批准|通过|approve/i })
  const reject = screen.queryByRole('button', { name: /拒绝|reject/i })
  if (role === 'admin') { expect(approve).toBeTruthy(); expect(reject).toBeTruthy() }
  else { expect(approve).toBeNull(); expect(reject).toBeNull() }
})
