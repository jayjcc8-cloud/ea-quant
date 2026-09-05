const canonicalDecimal = /^(?:0|-?(?:[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]))$/

function parse(value: string): { coefficient: bigint; scale: number } {
  if (!canonicalDecimal.test(value)) throw new Error('invalid canonical decimal')
  const negative = value.startsWith('-')
  const unsigned = negative ? value.slice(1) : value
  const [integer, fraction = ''] = unsigned.split('.')
  const coefficient = BigInt(`${integer}${fraction}`)
  return { coefficient: negative ? -coefficient : coefficient, scale: fraction.length }
}

function power10(exponent: number): bigint {
  return 10n ** BigInt(exponent)
}

function format(coefficient: bigint, scale: number): string {
  if (coefficient === 0n) return '0'
  let absolute = coefficient < 0n ? -coefficient : coefficient
  while (scale > 0 && absolute % 10n === 0n) {
    absolute /= 10n
    scale -= 1
  }
  const digits = absolute.toString().padStart(scale + 1, '0')
  const magnitude = scale === 0 ? digits : `${digits.slice(0, -scale)}.${digits.slice(-scale)}`
  return coefficient < 0n ? `-${magnitude}` : `+${magnitude}`
}

export function exactDelta(before: string, after: string, decimalShift = 0): string {
  if (!Number.isSafeInteger(decimalShift) || decimalShift < 0) {
    throw new Error('decimal shift must be a non-negative safe integer')
  }
  const left = parse(before)
  const right = parse(after)
  let scale = Math.max(left.scale, right.scale)
  let difference =
    right.coefficient * power10(scale - right.scale)
    - left.coefficient * power10(scale - left.scale)
  if (decimalShift > scale) {
    difference *= power10(decimalShift - scale)
    scale = 0
  } else {
    scale -= decimalShift
  }
  return format(difference, scale)
}
