import { describe, expect, it } from 'vitest'
import type { PaperInstance } from './paper-api'
import { paperObservation } from './paper-observation'

const row: PaperInstance = {
  run_id: 'run-a', request: { request_id: 'run-a', name: 'fixture', candidate_id: 'candidate', fingerprint: '', prices: [100], interval_seconds: 1 }, source_job_id: 'source',
  state: 'running', terminal_confirmed: false, stop_requested: false, created_at: '2026-01-02T00:00:00Z', observed_at: '2026-01-02T00:00:01Z',
  overview: { available: true, run_id: 'run-a', lease_held: true, health: {
    schema: 'ea.paper-health.v1', run_id: 'run-a', observed_at: '2026-01-02T00:00:00Z', process_alive: true, projection_stale: false,
    runtime_ready: true, trade_permitted: false, reason_codes: ['trade_permission.kill_switch'], trade_blocking_guard: 'kill_switch', trade_reason: 'operator requested halt', trade_permission_basis: 'zero_effect_probe',
  } },
}
const changed = (values: Record<string, unknown>) => ({ ...row, overview: { ...row.overview, health: { ...row.overview.health, ...values } } }) as PaperInstance

describe('read-only runtime observation', () => {
  it('separates a live ready process from blocked operational permission', () => {
    const value = paperObservation(row, false)
    expect(value.current).toBe(true)
    expect(value.process.text).toBe('存活 · 租约占用')
    expect(value.readiness.text).toBe('读取时就绪')
    expect(value.permission.text).toBe('读取时受阻')
    expect(value.reasons.join(' ')).toContain('operator requested halt')
    expect(value.reasons.join(' ')).toContain('kill_switch')
  })
  it('displays authoritative readiness reasons without deriving trade permission', () => {
    const value = paperObservation(changed({ runtime_ready: false, trade_permitted: true, reason_codes: ['readiness.storage'], storage_state: 'unavailable', trade_reason: null, trade_blocking_guard: null }), false)
    expect(value.readiness.text).toBe('读取时未就绪')
    expect(value.permission.text).toBe('读取时未阻断')
    expect(value.reasons.join(' ')).toContain('readiness.storage')
  })
  it.each([{ projection_stale: true }, { projection_stale: undefined }, { observed_at: null }])('never calls an unconfirmed projection current: %j', values => {
    const value = paperObservation(changed({ trade_permitted: true, ...values }), false)
    expect(value.current).toBe(false)
    expect(value.permission.tone).not.toBe('good')
    expect(value.readiness.tone).not.toBe('good')
  })
  it('demotes disconnected readings, but keeps publication time and prior reasons in diagnostics', () => {
    const value = paperObservation(row, true)
    expect(value.kind).toBe('disconnected')
    expect(value.process.text).toBe('未知')
    expect(value.reasons).toEqual([])
    expect(value.healthAt).not.toBe('未提供')
    expect(JSON.stringify(value.diagnostics)).toContain('operator requested halt')
  })
  it('treats confirmed stopped runs as history even when the reader marks their projection stale', () => {
    const historical = changed({ projection_stale: true, process_alive: false, runtime_ready: false, reason_codes: ['projection.stale', 'liveness.not_alive'] })
    historical.state = 'stopped'; historical.terminal_confirmed = true; historical.overview.lease_held = false
    const value = paperObservation(historical, false)
    expect(value.kind).toBe('history')
    expect(value.permission.text).toBe('历史观测')
    expect(value.reasons).toEqual([])
    expect(value.context).not.toContain('陈旧')
  })
  it('keeps missing booleans unknown, and quarantines foreign run health and overview', () => {
    const missing = paperObservation(changed({ runtime_ready: undefined, trade_permitted: undefined }), false)
    expect(missing.readiness.text).toBe('未知')
    expect(missing.permission.text).toBe('未知')
    const foreign = paperObservation(changed({ run_id: 'run-b', trade_reason: 'foreign reason' }), false)
    expect(foreign.health).toBeNull()
    expect(JSON.stringify(foreign)).not.toContain('foreign reason')
    expect(paperObservation({ ...row, overview: { ...row.overview, run_id: 'run-b' } }, false).overview).toBeUndefined()
  })
})
