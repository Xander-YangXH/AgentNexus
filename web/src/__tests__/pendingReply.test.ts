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
    p = applyRunEvent(p, { type: 'run.failed', data: { error_code: 'timeout' }, id: null })
    expect([p.status, p.text, p.errorCode]).toEqual(['failed', '半截', 'timeout'])
  })

  it('ignores events after a terminal state', () => {
    const done = applyRunEvent(newPending('r', 0), { type: 'run.failed', data: { error_code: 'x' }, id: null })
    expect(applyRunEvent(done, tok('late'))).toBe(done)
  })
})

describe('cancel and error codes', () => {
  it('cancelled keeps partial text', () => {
    let p = applyRunEvent(newPending('r', 0), tok('半'))
    p = applyRunEvent(p, { type: 'run.cancelled', data: {}, id: null })
    expect([p.status, p.text]).toEqual(['cancelled', '半'])
  })

  it('failed stores the error code, defaulting to internal', () => {
    const p = applyRunEvent(newPending('r', 0), { type: 'run.failed', data: { error_code: null }, id: null })
    expect([p.status, p.errorCode]).toEqual(['failed', 'internal'])
  })
})

describe('knowledge search events', () => {
  it('clears pre-tool text, shows activity, then streams the answer', () => {
    let p = applyRunEvent(newPending('r', 0), tok('我查一下'))
    p = applyRunEvent(p, { type: 'tool.started', data: { name: 'knowledge_search', query: '退货政策' }, id: null })
    expect([p.status, p.text, p.activity]).toEqual(['waiting', '', '正在检索知识库：退货政策'])
    p = applyRunEvent(p, { type: 'tool.finished', data: { name: 'knowledge_search', hits: 3, ok: true }, id: null })
    expect(p.activity).toBe('检索到 3 条资料，正在生成回答')
    p = applyRunEvent(p, tok('签收后'))
    expect([p.status, p.text, p.activity]).toEqual(['streaming', '签收后', null])
  })

  it('reports a failed search and ignores unknown event types', () => {
    let p = applyRunEvent(newPending('r', 0), { type: 'tool.finished', data: { name: 'knowledge_search', hits: 0, ok: false }, id: null })
    expect(p.activity).toBe('知识库检索失败，正在生成回答')
    const unknown = { type: 'something.new', data: {}, id: null } as unknown as RunEvent
    expect(applyRunEvent(p, unknown)).toBe(p)
  })
})

describe('agent tools (decisions/0021)', () => {
  const started = (name: string, detail: string) =>
    ({ type: 'tool.started', data: { name, query: '', detail }, id: null }) as RunEvent
  const finished = (name: string, ok: boolean) =>
    ({ type: 'tool.finished', data: { name, hits: 0, ok }, id: null }) as RunEvent

  it('labels each tool by name with its argument summary', () => {
    expect(applyRunEvent(newPending('r', 0), started('python_exec', 'import pandas as pd')).activity)
      .toBe('正在执行代码：import pandas as pd')
    expect(applyRunEvent(newPending('r', 0), started('fetch', 'https://example.com')).activity)
      .toBe('正在抓取网页：https://example.com')
    expect(applyRunEvent(newPending('r', 0), started('load_skill', 'data-analysis')).activity)
      .toBe('正在加载技能：data-analysis')
    expect(applyRunEvent(newPending('r', 0), started('mystery', '')).activity).toBe('正在调用 mystery')
  })

  it('reports completion and failure of non-search tools', () => {
    const p = applyRunEvent(newPending('r', 0), started('python_exec', 'x'))
    expect(applyRunEvent(p, finished('python_exec', true)).activity).toBe('代码执行完成，继续处理')
    expect(applyRunEvent(p, finished('python_exec', false)).activity).toBe('python_exec 执行失败，正在处理')
  })

  it('falls back to query when an older backend sends no detail', () => {
    const old = { type: 'tool.started', data: { name: 'knowledge_search', query: '退货' }, id: null } as RunEvent
    expect(applyRunEvent(newPending('r', 0), old).activity).toBe('正在检索知识库：退货')
  })
})

describe('takeover (run.resumed)', () => {
  it('clears the partial text and ignores late events from the previous attempt', () => {
    let p = applyRunEvent(newPending('r', 0), tok('第一次执行的半截'))
    p = applyRunEvent(p, { type: 'run.resumed', data: { attempt: 2 }, id: null })
    expect([p.status, p.text, p.attempt]).toEqual(['waiting', '', 2])
    expect(p.activity).toContain('接着生成')
    // 旧 worker 还没发现自己被接管，又流出一个 token（attempt 1 或不带 attempt 的第一次执行）
    const late = { type: 'token', data: { text: '旧', attempt: 1 }, id: null } as RunEvent
    expect(applyRunEvent(p, late)).toBe(p)
    expect(applyRunEvent(p, tok('旧，第一次执行不带 attempt'))).toBe(p)
    p = applyRunEvent(p, { type: 'token', data: { text: '新的', attempt: 2 }, id: null })
    expect([p.status, p.text, p.activity]).toEqual(['streaming', '新的', null])
    // 重复或乱序的 resumed（更小的 attempt）不会把状态倒回去
    expect(applyRunEvent(p, { type: 'run.resumed', data: { attempt: 2 }, id: null })).toBe(p)
    // 第一次执行的普通流程不受影响（tok 不带 attempt）
    expect(applyRunEvent(newPending('x', 0), tok('正常')).text).toBe('正常')
  })
})
