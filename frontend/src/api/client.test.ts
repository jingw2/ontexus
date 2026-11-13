import { afterAll, afterEach, beforeAll, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { apiClient, apiClientV2 } from './client'
import { useAuthStore } from '@/stores/authStore'

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterAll(() => server.close())
afterEach(() => {
  server.resetHandlers()
  vi.restoreAllMocks()
  useAuthStore.setState({ token: null, user: null })
})

it.each([['v1', apiClient], ['v2', apiClientV2]] as const)('%s permission denial preserves login without refreshing', async (version, client) => {
  useAuthStore.setState({ token: 'valid-session' })
  const fetch = vi.spyOn(globalThis, 'fetch')
  server.use(http.get(`*/api/${version}/denied`, () => HttpResponse.json({ detail: 'Forbidden' }, { status: 403 })))
  await expect(client.get('/denied')).rejects.toMatchObject({ detail: 'Forbidden' })
  expect(useAuthStore.getState().token).toBe('valid-session')
  expect(fetch).not.toHaveBeenCalled()
})

it('refreshes an expired session once and retries with the new token', async () => {
  useAuthStore.setState({ token: 'expired' })
  const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ data: { access_token: 'renewed' } }), { status: 200 }))
  server.use(http.get('*/api/v1/protected', ({ request }) => request.headers.get('Authorization') === 'Bearer renewed'
    ? HttpResponse.json({ data: { ok: true } }) : HttpResponse.json({}, { status: 401 })))
  await expect(apiClient.get('/protected')).resolves.toEqual({ ok: true })
  expect(useAuthStore.getState().token).toBe('renewed')
  expect(fetch).toHaveBeenCalledTimes(1)
})

it('clears login if the refreshed token is still rejected', async () => {
  useAuthStore.setState({ token: 'expired' })
  const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ data: { access_token: 'rejected' } }), { status: 200 }))
  server.use(http.get('*/api/v1/protected', () => HttpResponse.json({ detail: 'Unauthorized' }, { status: 401 })))
  await expect(apiClient.get('/protected')).rejects.toMatchObject({ detail: 'Unauthorized' })
  expect(useAuthStore.getState().token).toBeNull()
  expect(fetch).toHaveBeenCalledTimes(1)
})
