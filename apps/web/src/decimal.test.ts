import { describe, expect, it } from 'vitest'
import { exactDelta } from './decimal'

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
})
