// Settings dialog: the missing token-entry flow.
//
// Before this, the only way to authenticate the dashboard was to open devtools
// and run `localStorage.setItem('autopr_api_token', …)` — which meant the
// documented "set AUTOPR_API_TOKEN" deployment was unusable from the UI, and an
// operator's only recourse on a 401 was a rebuild. Now the token is entered here,
// verified against the API before it is saved, and the app reloads so every
// polling hook picks up the new credential.
import { useEffect, useRef, useState } from 'react'
import { clearToken, getToken, setToken, tokenSource, verifyToken } from '../api/client'
import { CheckCircleIcon, KeyIcon, ShieldIcon, XIcon } from './Icons'

interface Props {
  open: boolean
  onClose: () => void
}

const SOURCE_LABEL: Record<string, string> = {
  saved: 'saved in this browser',
  build: 'baked in at build time (VITE_AUTOPR_API_TOKEN)',
  none: 'no token — unauthenticated',
}

export default function SettingsDialog({ open, onClose }: Props) {
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ tone: 'ok' | 'error'; text: string } | null>(null)
  const source = tokenSource()
  const inputRef = useRef<HTMLInputElement>(null)

  // Reset the form each time the dialog opens; never pre-fill the secret itself
  // (a shoulder-surfer or a screen recording shouldn't capture it).
  useEffect(() => {
    if (!open) return
    setValue('')
    setMessage(null)
    setBusy(false)
    inputRef.current?.focus()
  }, [open])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  async function saveWithCheck() {
    const candidate = value.trim()
    if (!candidate) {
      setMessage({ tone: 'error', text: 'Enter a token first.' })
      return
    }
    setBusy(true)
    setMessage(null)
    const result = await verifyToken(candidate)
    if (!result.ok) {
      setBusy(false)
      setMessage({
        tone: 'error',
        text: `Token rejected — ${result.detail}. Nothing was saved.`,
      })
      return
    }
    setToken(candidate)
    setMessage({ tone: 'ok', text: 'Token verified and saved. Reloading…' })
    // A full reload is deliberate: every page owns its own polling loop, so
    // this is the one way to guarantee no stale unauthenticated cache remains.
    window.setTimeout(() => window.location.reload(), 400)
  }

  function clear() {
    clearToken()
    setValue('')
    setMessage({ tone: 'ok', text: 'Token cleared. Reloading…' })
    window.setTimeout(() => window.location.reload(), 400)
  }

  const hasToken = Boolean(getToken())

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-canvas/80 p-4 backdrop-blur-sm"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
        className="card w-full max-w-md p-6"
      >
        <div className="flex items-start justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="grid h-9 w-9 place-items-center rounded-lg bg-gold-line/40 text-gold-300 ring-1 ring-line">
              <KeyIcon className="h-4 w-4" />
            </div>
            <div>
              <h2 id="settings-title" className="font-display text-lg font-semibold text-ink">
                API access
              </h2>
              <p className="text-xs text-ink-faint">
                Stored locally in this browser. Never sent anywhere but your AutoPR API.
              </p>
            </div>
          </div>
          <button onClick={onClose} className="btn-ghost btn-sm" aria-label="Close settings">
            <XIcon className="h-4 w-4" />
          </button>
        </div>

        <div className="mt-4 flex items-center gap-2 rounded-lg border border-line bg-surface-2 px-3 py-2 text-xs text-ink-dim">
          <ShieldIcon className="h-3.5 w-3.5 shrink-0 text-risk-low" />
          <span>
            Current credential: <span className="text-ink">{SOURCE_LABEL[source]}</span>
          </span>
        </div>

        <label htmlFor="api-token" className="mt-4 block text-xs font-medium text-ink-dim">
          API token
        </label>
        <input
          id="api-token"
          ref={inputRef}
          type="password"
          autoComplete="off"
          spellCheck={false}
          placeholder="paste AUTOPR_API_TOKEN"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void saveWithCheck()
          }}
          className="input mt-1.5 w-full font-mono text-sm"
        />
        <p className="mt-1.5 text-[11px] text-ink-faint">
          Required when the server sets <code className="font-mono">AUTOPR_API_TOKEN</code> — it
          protects approve/reject and, if enabled, reads too.
        </p>

        {message && (
          <div
            role={message.tone === 'ok' ? 'status' : 'alert'}
            className={`mt-3 flex items-start gap-2 rounded-lg border px-3 py-2 text-xs ${
              message.tone === 'ok'
                ? 'border-risk-low/30 bg-risk-low/10 text-risk-low'
                : 'border-risk-high/30 bg-risk-high/10 text-risk-high'
            }`}
          >
            {message.tone === 'ok' && <CheckCircleIcon className="mt-px h-3.5 w-3.5 shrink-0" />}
            <span>{message.text}</span>
          </div>
        )}

        <div className="mt-5 flex items-center gap-2">
          <button
            onClick={() => void saveWithCheck()}
            disabled={busy}
            className="btn-primary btn-sm"
          >
            {busy ? 'Checking…' : 'Verify & save'}
          </button>
          {hasToken && (
            <button onClick={clear} disabled={busy} className="btn-danger btn-sm">
              Clear token
            </button>
          )}
          <button onClick={onClose} className="btn-ghost btn-sm ml-auto">
            Cancel
          </button>
        </div>
      </div>
    </div>
  )
}
