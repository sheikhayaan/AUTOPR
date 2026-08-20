import { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react'
import { CheckCircleIcon, AlertIcon, InfoIcon, XIcon } from '../components/Icons.jsx'

const ToastContext = createContext(null)

const TONES = {
  success: { icon: CheckCircleIcon, accent: 'text-risk-low', ring: 'ring-risk-low/25' },
  error: { icon: AlertIcon, accent: 'text-risk-high', ring: 'ring-risk-high/25' },
  info: { icon: InfoIcon, accent: 'text-brand-400', ring: 'ring-brand-500/25' },
}

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])
  const seq = useRef(0)

  const dismiss = useCallback((id) => {
    setToasts((t) => t.filter((x) => x.id !== id))
  }, [])

  const push = useCallback(
    (toast) => {
      const id = ++seq.current
      const entry = { id, type: 'info', duration: 4000, ...toast }
      setToasts((t) => [...t, entry])
      if (entry.duration > 0) setTimeout(() => dismiss(id), entry.duration)
      return id
    },
    [dismiss],
  )

  const value = useMemo(
    () => ({
      push,
      dismiss,
      success: (message, opts) => push({ ...opts, type: 'success', message }),
      error: (message, opts) => push({ ...opts, type: 'error', message }),
      info: (message, opts) => push({ ...opts, type: 'info', message }),
    }),
    [push, dismiss],
  )

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="pointer-events-none fixed bottom-5 right-5 z-50 flex w-[min(92vw,22rem)] flex-col gap-2.5">
        {toasts.map((t) => {
          const tone = TONES[t.type] || TONES.info
          const Icon = tone.icon
          return (
            <div
              key={t.id}
              role="status"
              className={`pointer-events-auto flex items-start gap-3 rounded-xl border border-line bg-surface-2/95 p-3.5 shadow-pop ring-1 ${tone.ring} backdrop-blur animate-fade-up`}
            >
              <Icon className={`mt-0.5 h-5 w-5 shrink-0 ${tone.accent}`} />
              <div className="min-w-0 flex-1">
                {t.title && <p className="text-sm font-semibold text-ink">{t.title}</p>}
                <p className="text-sm text-ink-dim">{t.message}</p>
              </div>
              <button
                onClick={() => dismiss(t.id)}
                className="text-ink-faint transition-colors hover:text-ink"
                aria-label="Dismiss"
              >
                <XIcon className="h-4 w-4" />
              </button>
            </div>
          )
        })}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast() {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used within a ToastProvider')
  return ctx
}
