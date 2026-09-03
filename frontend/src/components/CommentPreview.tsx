import type { ReactNode } from 'react'
import { LogoMark } from './Icons'

// Render the inline formatting policy.py emits: **bold** and `code`.
// Builds React nodes (never raw HTML) so LLM-authored finding text can't inject
// markup — React escapes all text content.
function renderInline(text: string, keyBase: string): ReactNode[] {
  const nodes: ReactNode[] = []
  const re = /(\*\*([^*]+)\*\*|`([^`]+)`)/g
  let last = 0
  let m: RegExpExecArray | null
  let i = 0
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.slice(last, m.index))
    if (m[2] !== undefined) {
      nodes.push(
        <strong key={`${keyBase}-b${i}`} className="font-semibold text-ink">
          {m[2]}
        </strong>,
      )
    } else if (m[3] !== undefined) {
      nodes.push(
        <code
          key={`${keyBase}-c${i}`}
          className="rounded bg-white/[0.08] px-1.5 py-0.5 font-mono text-[0.85em] text-brand-200"
        >
          {m[3]}
        </code>,
      )
    }
    last = m.index + m[0].length
    i++
  }
  if (last < text.length) nodes.push(text.slice(last))
  return nodes
}

function CodeBlock({ lang, lines }: { lang: string; lines: string[] }) {
  const isDiff = lang === 'diff'
  return (
    <pre className="my-3 overflow-x-auto rounded-lg border border-line bg-canvas/80 p-3 text-[12.5px] leading-relaxed">
      <code className="font-mono">
        {lines.map((ln, i) => {
          let cls = 'text-ink-dim'
          if (isDiff) {
            if (ln.startsWith('+')) cls = 'text-risk-low'
            else if (ln.startsWith('-')) cls = 'text-risk-high'
            else if (ln.startsWith('@@')) cls = 'text-risk-trivial'
          }
          return (
            <div key={i} className={cls}>
              {ln || ' '}
            </div>
          )
        })}
      </code>
    </pre>
  )
}

interface Fence {
  lang: string
  lines: string[]
}

// Minimal, targeted markdown renderer for the decision bodies. Handles exactly
// what policy.py produces: ##/### headings, - bullets, ```fences``` (with diff
// coloring), **bold**, `code`, and it quietly tolerates the <details>/<summary>
// wrappers used in escalation bodies.
function renderMarkdown(body: string | null | undefined): ReactNode[] {
  const lines = (body || '').split('\n')
  const blocks: ReactNode[] = []
  let list: string[] | null = null
  let fence: Fence | null = null
  let para: string | null = null

  const flushList = () => {
    if (list) {
      blocks.push(
        <ul key={`ul${blocks.length}`} className="my-2 space-y-1.5 pl-1">
          {list.map((item, i) => (
            <li key={i} className="flex gap-2 text-sm text-ink-dim">
              <span className="mt-2 h-1 w-1 shrink-0 rounded-full bg-ink-faint" />
              <span>{renderInline(item, `li${blocks.length}-${i}`)}</span>
            </li>
          ))}
        </ul>,
      )
      list = null
    }
  }
  const flushPara = () => {
    if (para && para.trim()) {
      blocks.push(
        <p key={`p${blocks.length}`} className="my-2 text-sm leading-relaxed text-ink-dim">
          {renderInline(para.trim(), `p${blocks.length}`)}
        </p>,
      )
    }
    para = null
  }

  for (const raw of lines) {
    const line = raw.replace(/\s+$/, '')

    if (fence) {
      if (line.trim().startsWith('```')) {
        blocks.push(
          <CodeBlock key={`code${blocks.length}`} lang={fence.lang} lines={fence.lines} />,
        )
        fence = null
      } else {
        fence.lines.push(raw)
      }
      continue
    }
    if (line.trim().startsWith('```')) {
      flushPara()
      flushList()
      fence = { lang: line.trim().slice(3).trim(), lines: [] }
      continue
    }

    // Structural HTML wrappers in escalation bodies.
    if (/^<\/?details>/.test(line.trim())) {
      flushPara()
      flushList()
      continue
    }
    const summary = line.trim().match(/^<summary>(.*)<\/summary>$/)
    if (summary) {
      flushPara()
      flushList()
      blocks.push(
        <p
          key={`s${blocks.length}`}
          className="mt-3 text-xs font-semibold uppercase tracking-wide text-ink-faint"
        >
          {summary[1]}
        </p>,
      )
      continue
    }

    if (line.startsWith('### ')) {
      flushPara()
      flushList()
      blocks.push(
        <h4 key={`h${blocks.length}`} className="mb-1 mt-4 text-sm font-semibold text-ink">
          {renderInline(line.slice(4), `h${blocks.length}`)}
        </h4>,
      )
    } else if (line.startsWith('## ')) {
      flushPara()
      flushList()
      blocks.push(
        <h3 key={`h${blocks.length}`} className="mb-2 mt-1 text-base font-semibold text-ink">
          {renderInline(line.slice(3), `h${blocks.length}`)}
        </h3>,
      )
    } else if (line.startsWith('- ')) {
      flushPara()
      if (!list) list = []
      list.push(line.slice(2))
    } else if (line.trim() === '') {
      flushPara()
      flushList()
    } else {
      flushList()
      para = para ? `${para} ${line}` : line
    }
  }
  flushPara()
  flushList()
  if (fence) blocks.push(<CodeBlock key="codeEnd" lang={fence.lang} lines={fence.lines} />)
  return blocks
}

// A faithful preview of the comment AutoPR would post to the PR, framed like a
// GitHub comment so a reviewer sees exactly what a maintainer would see.
export default function CommentPreview({ body }: { body: string | null | undefined }) {
  return (
    <div className="overflow-hidden rounded-xl border border-line bg-surface-2">
      <div className="flex items-center gap-2.5 border-b border-line bg-white/[0.02] px-4 py-2.5">
        <LogoMark className="h-6 w-6" />
        <span className="text-sm font-medium text-ink">AutoPR bot</span>
        <span className="text-sm text-ink-faint">would comment</span>
        <span className="chip ml-auto border-brand-500/25 bg-brand-500/10 px-2 py-0.5 text-[10px] uppercase tracking-wide text-brand-400">
          Preview
        </span>
      </div>
      <div className="px-4 py-3">
        {body ? (
          renderMarkdown(body)
        ) : (
          <p className="py-4 text-center text-sm text-ink-faint">No body was generated.</p>
        )}
      </div>
    </div>
  )
}
