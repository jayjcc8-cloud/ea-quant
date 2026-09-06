import { describe, expect, it } from 'vitest'
import { compareCanonicalDecimal, exactDelta } from './decimal'

describe('exactDelta', () => {
  it('subtracts canonical decimal strings without floating point', () => {
    expect(exactDelta('10017', '10034')).toBe('+17')
    expect(exactDelta('17', '34')).toBe('+17')
    expect(exactDelta('34', '17')).toBe('-17')
    expect(exactDelta('1', '1')).toBe('0')
  })

  it('converts fractional-return difference to percentage points exactly', () => {
    expect(exactDelta('0.0017', '0.0034', 2)).toBe('+0.17')
    expect(exactDelta('0.0034', '0.0017', 2)).toBe('-0.17')
  })

  it('orders canonical decimal strings without floating point', () => {
    expect(compareCanonicalDecimal('2', '10')).toBeLessThan(0)
    expect(compareCanonicalDecimal('0.1', '0.1')).toBe(0)
    expect(compareCanonicalDecimal('-2.5', '-3')).toBeGreaterThan(0)
  })
})
