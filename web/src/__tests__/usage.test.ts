import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, onUnauthorized } from '../api/http'
import type { Me } from '../api/types'
import { fillDays, formatTokens, scopesFor } from '../composables/usage'

const me = (role: Me['role'], department_id: string | null = 'd1'): Me =>
  ({ id: 'u', name: 'n', role, department_id, department_name: null })

describe('usage scopes (decisions/0023)', () => {
  it('match what the backend allows for each role', () => {
    expect(scopesFor(me('member')).map((s) => s.value)).toEqual(['me'])
    expect(scopesFor(me('manager')).map((s) => s.value)).toEqual(['me', 'department'])
    expect(scopesFor(me('manager', null)).map((s) => s.value)).toEqual(['me']) // 没有部门的负责人没有“部门”可看
    expect(scopesFor(me('admin', null)).map((s) => s.value)).toEqual(['me', 'department', 'all'])
  })
})

describe('fillDays', () => {
  it('fills missing days with zeros so the line does not jump across gaps', () => {
    const row = { runs: 1, llm_calls: 2, searches: 0, input_tokens: 10, cached_tokens: 0, output_tokens: 1, cost_usd: 0 }
    const out = fillDays([{ ...row, day: '2026-10-21' }], 3, new Date('2026-10-23T08:00:00Z'))
    expect(out.map((r) => [r.day, r.llm_calls])).toEqual([['2026-10-21', 2], ['2026-10-22', 0], ['2026-10-23', 0]])
  })

  it('formats token counts', () => {
    expect([formatTokens(950), formatTokens(12_300), formatTokens(2_500_000)]).toEqual(['950', '12.3k', '2.5M'])
  })
})

describe('session expiry', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('notifies the app on 401 except for the login request itself', async () => {
    const handler = vi.fn()
    onUnauthorized(handler)
    const body = '{"error":{"code":"unauthorized","message":"not logged in","request_id":"r"}}'
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(new Response(body, { status: 401 }))))
    await expect(api.listConversations()).rejects.toThrow()
    expect(handler).toHaveBeenCalledTimes(1)
    await expect(api.login('a', 'b')).rejects.toThrow() // 密码错误不是“会话失效”
    expect(handler).toHaveBeenCalledTimes(1)
  })
})
