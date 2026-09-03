import { Component, type ErrorInfo, type ReactNode } from 'react'
import { AlertIcon } from './Icons'

interface Props {
  children: ReactNode
}
interface State {
  error: Error | null
}

// A top-level React error boundary. A render-time exception anywhere in the
// tree is caught here and turned into a recoverable screen instead of a blank
// white page — the honest failure mode for a control plane. This is display-only
// safety: no pipeline state is touched.
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // In a real deployment this would report to Sentry/etc. Here we surface it
    // to the console so it is never silently swallowed.
    console.error('AutoPR dashboard crashed:', error, info.componentStack)
  }

  private handleReload = () => {
    window.location.reload()
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div className="grid min-h-screen place-items-center bg-canvas px-6">
          <div className="card max-w-md p-8 text-center">
            <div className="mx-auto mb-4 grid h-14 w-14 place-items-center rounded-2xl bg-risk-high/10 text-risk-high ring-1 ring-risk-high/20">
              <AlertIcon className="h-7 w-7" />
            </div>
            <h1 className="font-display text-xl font-semibold text-ink">Something went wrong</h1>
            <p className="mt-2 text-sm text-ink-dim">
              The dashboard hit an unexpected error and stopped rendering. Your data is safe — this
              is a display-only fault.
            </p>
            {this.state.error.message && (
              <pre className="mt-4 overflow-x-auto rounded-lg border border-line bg-surface-2 p-3 text-left text-xs text-ink-faint">
                {this.state.error.message}
              </pre>
            )}
            <button onClick={this.handleReload} className="btn-royal btn-sm mx-auto mt-6">
              Reload dashboard
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
