import axios, { type AxiosRequestConfig } from 'axios'
import { useAuthStore } from '@/stores/authStore'
import { useRuntimeDelegationStore } from '@/stores/runtimeDelegationStore'

type ApiClient = {
  get: <T = unknown>(url: string, config?: AxiosRequestConfig) => Promise<T>
  post: <T = unknown>(url: string, data?: unknown, config?: AxiosRequestConfig) => Promise<T>
  put: <T = unknown>(url: string, data?: unknown, config?: AxiosRequestConfig) => Promise<T>
  patch: <T = unknown>(url: string, data?: unknown, config?: AxiosRequestConfig) => Promise<T>
  delete: <T = unknown>(url: string, config?: AxiosRequestConfig) => Promise<T>
}

type RetriableConfig = AxiosRequestConfig & { _retried?: boolean }

export function readCookie(name: string): string | null {
  const match = document.cookie.match(new RegExp(`(?:^|;\\s*)${name}=([^;]*)`))
  return match ? decodeURIComponent(match[1] ?? '') : null
}

let refreshing: Promise<string | null> | null = null

export function refreshAccessToken(): Promise<string | null> {
  if (refreshing) return refreshing
  refreshing = (async () => {
    try {
      const csrf = readCookie('csrf_token') ?? ''
      const response = await fetch('/api/v1/auth/refresh', {
        method: 'POST',
        headers: { 'X-CSRF-Token': csrf },
        credentials: 'same-origin',
      })
      if (!response.ok) return null
      const body = (await response.json()) as { data?: { access_token?: string } }
      const token = body?.data?.access_token ?? null
      if (token) useAuthStore.getState().setToken(token)
      return token
    } catch {
      return null
    } finally {
      refreshing = null
    }
  })()
  return refreshing
}

function createApiClient(baseURL: string, options?: { runtimeDelegation?: boolean, preserveSessionOnAuthorizationFailure?: boolean }): ApiClient {
  const client = axios.create({ baseURL })
  client.interceptors.request.use(config => {
    const token = options?.runtimeDelegation
      ? useRuntimeDelegationStore.getState().token
      : useAuthStore.getState().token
    if (token) config.headers.Authorization = `Bearer ${token}`
    return config
  })
  client.interceptors.response.use(
    res => (res.data?.data !== undefined ? res.data.data : res.data),
    async err => {
      const config = err.config as RetriableConfig | undefined
      const status = err.response?.status
      if (!options?.runtimeDelegation && !options?.preserveSessionOnAuthorizationFailure && status === 401 && config && !config._retried) {
        config._retried = true
        const token = await refreshAccessToken()
        if (token) {
          config.headers = { ...config.headers, Authorization: `Bearer ${token}` }
          return client(config)
        }
      }
      if (!options?.runtimeDelegation && !options?.preserveSessionOnAuthorizationFailure && status === 401) {
        useAuthStore.getState().logout()
      }
      if (options?.runtimeDelegation && status === 401) {
        // The delegated credential itself has expired or been rejected —
        // drop it so `RuntimeDelegationGate` re-prompts for a fresh
        // delegation instead of the page failing repeatedly with a stale
        // token. This is a Runtime-credential denial, not evidence the
        // ordinary session is invalid, so the session bearer is untouched.
        useRuntimeDelegationStore.getState().clear()
      }
      return Promise.reject(err.response?.data ?? err)
    }
  )
  return {
    // The response interceptor unwraps ApiEnvelope data; the runtime value is T.
    get: <T = unknown>(url: string, config?: AxiosRequestConfig) => client.get<T>(url, config) as unknown as Promise<T>,
    post: <T = unknown>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
      client.post<T>(url, data, config) as unknown as Promise<T>,
    put: <T = unknown>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
      client.put<T>(url, data, config) as unknown as Promise<T>,
    patch: <T = unknown>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
      client.patch<T>(url, data, config) as unknown as Promise<T>,
    delete: <T = unknown>(url: string, config?: AxiosRequestConfig) => client.delete<T>(url, config) as unknown as Promise<T>,
  }
}

export const apiClient = createApiClient('/api/v1')
export const apiClientV2 = createApiClient('/api/v2')
// Runtime credential denials are authorization results for a separate,
// short-lived delegated bearer. They must never clear the ordinary session.
export const runtimeApiClient = createApiClient('/api/v2', { runtimeDelegation: true })
// Delegation issuance authenticates with the ordinary session but a policy
// denial is not evidence that that session is invalid.
export const runtimeDelegationClient = createApiClient('/api/v2', { preserveSessionOnAuthorizationFailure: true })
