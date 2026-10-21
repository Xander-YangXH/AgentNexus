import { describe, expect, it } from 'vitest'
import type { RunEvent } from '../api/types'
import { applyRunEvent, newPending } from '../composables/pendingReply'

const tok = (text: string): RunEvent => ({ type: 'token', data: { text }, id: null })

describe('pending reply state machine', () => {
  it('waiting -> streaming -> completed', () => {
    let p = newPending('r', 0)
    expect(p.status).toBe('waiting')
    p = applyRunEvent(p, { type: 'run.started', data: {}, id: null })
    expect(p.status).toBe('waiting') // 开始执行不等于开始输出
    p = applyRunEvent(applyRunEvent(p, tok('你')), tok('好'))
    expect([p.status, p.text]).toEqual(['streaming', '你好'])
    p = applyRunEvent(p, { type: 'run.completed', data: { message_id: 'm' }, id: null })
    expect(p.status).toBe('completed')
  })

  it('failed keeps the partial text and the error', () => {
    let p = applyRunEvent(newPending('r', 0), tok('半截'))
    p = applyRunEvent(p, { type: 'run.failed', data: { error: 'timeout after 300s' }, id: null })
    expect([p.status, p.text, p.error]).toEqual(['failed', '半截', 'timeout after 300s'])
  })

  it('ignores events after a terminal state', () => {
    const done = applyRunEvent(newPending('r', 0), { type: 'run.failed', data: { error: 'x' }, id: null })
    expect(applyRunEvent(done, tok('late'))).toBe(done)
  })
})
