import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  actionLabel,
  pluralize,
  reasonLabel,
  relativeTime,
  riskMeta,
  shortSha,
  statusMeta,
  toneBg,
} from './format'

describe('shortSha', () => {
  it('takes the first 7 chars', () => {
    expect(shortSha('abcdef1234567')).toBe('abcdef1')
  })
  it('is safe on null/empty', () => {
    expect(shortSha(null)).toBe('')
    expect(shortSha(undefined)).toBe('')
  })
})

describe('pluralize', () => {
  it('keeps the singular for 1', () => expect(pluralize(1, 'job')).toBe('1 job'))
  it('adds -s by default', () => expect(pluralize(2, 'job')).toBe('2 jobs'))
  it('honors an explicit plural', () => expect(pluralize(0, 'entry', 'entries')).toBe('0 entries'))
})

describe('riskMeta', () => {
  it('maps known levels', () => expect(riskMeta('high').label).toBe('High'))
  it('echoes unknown levels', () => expect(riskMeta('spicy').label).toBe('spicy'))
  it('falls back to Unknown on null', () => expect(riskMeta(null).label).toBe('Unknown'))
})

describe('statusMeta', () => {
  it('maps processing to a pulsing amber tone', () => {
    const m = statusMeta('processing')
    expect(m.tone).toBe('amber')
    expect(m.pulse).toBe(true)
  })
  it('defaults unknown statuses to slate', () => {
    expect(statusMeta('mystery').tone).toBe('slate')
  })
})

describe('toneBg', () => {
  it('maps a known tone', () => expect(toneBg('green')).toBe('bg-risk-low'))
  it('falls back for an unknown tone', () => expect(toneBg('chartreuse')).toBe('bg-ink-faint'))
})

describe('reasonLabel / actionLabel', () => {
  it('maps known reason codes', () =>
    expect(reasonLabel('low_risk_auto')).toBe('Low-risk · auto-posted'))
  it('humanizes unknown reason codes', () =>
    expect(reasonLabel('some_new_code')).toBe('some new code'))
  it('maps known actions', () => expect(actionLabel('propose_fix')).toBe('Propose fix'))
  it('dashes on null', () => expect(actionLabel(null)).toBe('—'))
})

describe('relativeTime', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
  })
  afterEach(() => vi.useRealTimers())

  it('dashes on null', () => expect(relativeTime(null)).toBe('—'))
  it('says "just now" under 5s', () =>
    expect(relativeTime('2025-12-31T23:59:58Z')).toBe('just now'))
  it('formats minutes', () => expect(relativeTime('2025-12-31T23:59:00Z')).toBe('1m ago'))
  it('formats hours', () => expect(relativeTime('2025-12-31T22:00:00Z')).toBe('2h ago'))
})
