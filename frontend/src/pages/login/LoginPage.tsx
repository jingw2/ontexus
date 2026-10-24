import { useForm } from 'react-hook-form'
import { useNavigate, useSearchParams, Link } from 'react-router-dom'
import { useAuthStore } from '@/stores/authStore'
import { authApi } from '@/api/auth'
import { useTranslation } from 'react-i18next'
import { useState } from 'react'

export default function LoginPage() {
  const { register, handleSubmit } = useForm<{ username: string; password: string }>()
  const setAuth = useAuthStore(s => s.setAuth)
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const { t } = useTranslation()
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const returnTo = searchParams.get('returnTo')
  const safeReturnTo = returnTo && returnTo.startsWith('/') && !returnTo.startsWith('//') && !returnTo.includes('\\')
    ? returnTo
    : ''
  const chatgptHref = safeReturnTo
    ? `/api/v1/auth/chatgpt/start?return_to=${encodeURIComponent(safeReturnTo)}`
    : '/api/v1/auth/chatgpt/start'
  const chatgptCode = searchParams.get('chatgpt_error')
  const chatgptError = chatgptCode ? t(`auth.chatgpt_error_${chatgptCode}`, { defaultValue: t('auth.chatgpt_error_failed') }) : ''

  const onSubmit = async (data: { username: string; password: string }) => {
    setLoading(true)
    setError('')
    try {
      const res = await authApi.login(data.username, data.password)
      useAuthStore.getState().setToken(res.access_token)
      const profile = await authApi.profile()
      setAuth(profile, res.access_token)
      // only ever navigate to a same-app relative path — never let a
      // crafted ?returnTo= value redirect off-site after login
      const returnTo = searchParams.get('returnTo')
      const isSafeReturnTo = returnTo && returnTo.startsWith('/') && !returnTo.startsWith('//')
      navigate(isSafeReturnTo ? returnTo : '/')
    } catch (e: unknown) {
      console.error('login failed:', e)
      const err = e as { isAxiosError?: boolean; response?: unknown }
      // The interceptor rejects with the response body for HTTP errors (no
      // `response`/`isAxiosError`); only a real transport failure keeps them.
      setError(err?.isAxiosError || err?.response ? t('auth.network_error') : t('auth.login_error'))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50">
      <div className="w-full max-w-sm bg-white rounded-lg shadow p-8">
        <h1 className="text-2xl font-semibold mb-2">Ontexus</h1>
        <p className="text-gray-500 text-sm mb-6">{t('auth.tagline')}</p>
        <a href={chatgptHref}
          className="block w-full text-center border border-black rounded-lg py-2 text-sm font-medium mb-4">
          {t('auth.continue_chatgpt')}
        </a>
        <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
          <div>
            <label className="block text-sm font-medium mb-1">{t('auth.username')}</label>
            <input {...register('username', { required: true })} placeholder={t('auth.ph_username')}
              className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-black" />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">{t('auth.password')}</label>
            <input {...register('password', { required: true })} type="password" placeholder={t('auth.ph_password')}
              className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-black" />
          </div>
          {(error || chatgptError) && <p className="text-red-500 text-sm">{error || chatgptError}</p>}
          <button type="submit" disabled={loading}
            className="w-full bg-black text-white rounded-lg py-2 text-sm font-medium disabled:opacity-50">
            {loading ? t('common.loading') : t('auth.login')}
          </button>
        </form>
        <p className="mt-4 text-sm text-center text-gray-500">
          {t('auth.no_account')} <Link to="/register" className="text-black underline">{t('auth.register')}</Link>
        </p>
      </div>
    </div>
  )
}
