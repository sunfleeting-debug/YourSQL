import { describe, expect, it } from 'vitest'
import { decodeManualPayload, encodeManualPayload } from './payload-codec'

const pythonVector =
  '5953504c07030000000507000000696e7465676572030905050000006974656d7306030000000200040000000000000440' + '0504000000746578740506000000e4b8ade69687'

function hex(raw: Uint8Array): string {
  return Array.from(raw, value => value.toString(16).padStart(2, '0')).join('')
}

function bytes(value: string): Uint8Array {
  return new Uint8Array(value.match(/.{2}/g)?.map(item => Number.parseInt(item, 16)) ?? [])
}

describe('manual payload codec', () => {
  it('matches the backend wire vector', () => {
    const value = { integer: -5, text: '中文', items: [true, null, 2.5] }

    expect(hex(encodeManualPayload(value))).toBe(pythonVector)
    expect(decodeManualPayload(bytes(pythonVector))).toEqual(value)
  })
})
