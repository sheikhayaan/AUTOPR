import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import SettingsDialog from './SettingsDialog'

// The dialog is the app's only token-entry path: before it, the documented
// `AUTOPR_API_TOKEN` deployment could not be used from the UI at all.
const { verifyMock } = vi.hoisted(() => ({ verifyMock: vi.fn() }))

vi.mock('../api/client', () => ({
  getToken: () => localStorage.getItem('autopr_api_token') ?? '',
  tokenSource: () => (localStorage.getItem('autopr_api_token') ? 'saved' : 'none'),
  setToken: (t: string) => {
    if (t) localStorage.setItem('autopr_api_token', t)
    else localStorage.removeItem('autopr_api_token')
  },
  clearToken: () => localStorage.removeItem('autopr_api_token'),
  verifyToken: verifyMock,
}))

const reload = vi.fn()

beforeEach(() => {
  localStorage.clear()
  reload.mockReset()
  // jsdom refuses to navigate; stub reload so the "save then reload" flow is
  // observable without killing the test environment.
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { ...window.location, reload },
  })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('SettingsDialog', () => {
  it('renders nothing while closed', () => {
    render(<SettingsDialog open={false} onClose={vi.fn()} />)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('shows the active credential source so the operator knows what is in use', () => {
    localStorage.setItem('autopr_api_token', 'abc')
    render(<SettingsDialog open onClose={vi.fn()} />)
    expect(screen.getByText('saved in this browser')).toBeInTheDocument()
  })

  it('says "unauthenticated" when no token is configured', () => {
    render(<SettingsDialog open onClose={vi.fn()} />)
    expect(screen.getByText('no token — unauthenticated')).toBeInTheDocument()
    // Nothing to clear yet.
    expect(screen.queryByRole('button', { name: /clear token/i })).not.toBeInTheDocument()
  })

  it('verifies before saving and persists only a token the API accepted', async () => {
    verifyMock.mockResolvedValue({ ok: true })
    render(<SettingsDialog open onClose={vi.fn()} />)

    await userEvent.type(screen.getByLabelText('API token'), 'good-token')
    await userEvent.click(screen.getByRole('button', { name: /verify & save/i }))

    await waitFor(() => expect(verifyMock).toHaveBeenCalledWith('good-token'))
    await waitFor(() => expect(localStorage.getItem('autopr_api_token')).toBe('good-token'))
    expect(await screen.findByRole('status')).toHaveTextContent(/verified and saved/i)
  })

  it('never persists a rejected token, and says why', async () => {
    verifyMock.mockResolvedValue({ ok: false, detail: 'Unauthorized' })
    render(<SettingsDialog open onClose={vi.fn()} />)

    await userEvent.type(screen.getByLabelText('API token'), 'bad-token')
    await userEvent.click(screen.getByRole('button', { name: /verify & save/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/token rejected — Unauthorized/i)
    expect(localStorage.getItem('autopr_api_token')).toBeNull()
  })

  it('refuses to submit an empty token', async () => {
    render(<SettingsDialog open onClose={vi.fn()} />)
    await userEvent.click(screen.getByRole('button', { name: /verify & save/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/enter a token/i)
    expect(verifyMock).not.toHaveBeenCalled()
  })

  it('clears a saved token', async () => {
    localStorage.setItem('autopr_api_token', 'abc')
    render(<SettingsDialog open onClose={vi.fn()} />)
    await userEvent.click(screen.getByRole('button', { name: /clear token/i }))
    await waitFor(() => expect(localStorage.getItem('autopr_api_token')).toBeNull())
  })

  it('closes on Escape and on the backdrop', async () => {
    const onClose = vi.fn()
    render(<SettingsDialog open onClose={onClose} />)
    await userEvent.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalled()
  })

  it('submits on Enter', async () => {
    verifyMock.mockResolvedValue({ ok: true })
    render(<SettingsDialog open onClose={vi.fn()} />)
    await userEvent.type(screen.getByLabelText('API token'), 'tok{Enter}')
    await waitFor(() => expect(verifyMock).toHaveBeenCalledWith('tok'))
  })
})
