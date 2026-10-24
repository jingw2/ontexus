import '@/i18n'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import LoginPage from './LoginPage'

function renderLogin(path = '/login') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <LoginPage />
    </MemoryRouter>,
  )
}

describe('LoginPage ChatGPT OAuth', () => {
  it('links to the ChatGPT authorize start endpoint', () => {
    renderLogin()
    expect(screen.getByRole('link', { name: '使用 ChatGPT 继续' })).toHaveAttribute(
      'href',
      '/api/v1/auth/chatgpt/start',
    )
  })

  it('forwards a same-app return path and drops an off-site one', () => {
    const { unmount } = renderLogin('/login?returnTo=%2Fontologies')
    expect(screen.getByRole('link', { name: '使用 ChatGPT 继续' })).toHaveAttribute(
      'href',
      '/api/v1/auth/chatgpt/start?return_to=%2Fontologies',
    )
    unmount()
    renderLogin('/login?returnTo=%2F%2Fevil.example')
    expect(screen.getByRole('link', { name: '使用 ChatGPT 继续' })).toHaveAttribute(
      'href',
      '/api/v1/auth/chatgpt/start',
    )
  })

  it('shows the ChatGPT sign-in error from the callback', () => {
    renderLogin('/login?chatgpt_error=email_in_use')
    expect(screen.getByText('该邮箱已有账号，请使用密码登录')).toBeInTheDocument()
  })
})
