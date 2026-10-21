import { describe, expect, it } from 'vitest'
import type { Message, Run } from '../api/types'
import { computeNotices } from '../composables/runNotices'
import { describeError } from '../utils/errors'

const msg = (id: string, role: Message['role'] = 'user'): Message => ({ id, seq: 0, role, content: id, created_at: '' })
const run = (id: string, input: string, status: Run['status'], created_at: string, error_code: string | null = null): Run => ({
  id,
  conversation_id: 'c',
  input_message_id: input,
  output_message_id: null,
  status,
  error_code,
  created_at,
  started_at: null,
  finished_at: null,
})

describe('computeNotices', () => {
  it('marks failed / cancelled user messages and only the last one is retryable', () => {
    const messages = [msg('m1'), msg('m2')]
    const runs = [run('r1', 'm1', 'failed', '1', 'llm_unavailable'), run('r2', 'm2', 'cancelled', '2')]
    const n = computeNotices(messages, runs)
    expect(n.get('m1')).toEqual({ kind: 'failed', errorCode: 'llm_unavailable', runId: 'r1', retryable: false })
    expect(n.get('m2')).toMatchObject({ kind: 'cancelled', retryable: true })
  })

  it('only looks at the latest run of a message (a successful retry clears the notice)', () => {
    const messages = [msg('m1'), msg('a1', 'assistant')]
    const runs = [run('r1', 'm1', 'failed', '1'), run('r2', 'm1', 'succeeded', '2')]
    expect(computeNotices(messages, runs).size).toBe(0)
  })

  it('a message followed by others is not retryable', () => {
    const n = computeNotices([msg('m1'), msg('m2')], [run('r1', 'm1', 'failed', '1')])
    expect(n.get('m1')?.retryable).toBe(false)
  })
})

describe('describeError', () => {
  it('maps codes to user-facing copy and never echoes unknown raw text', () => {
    expect(describeError('llm_unavailable')).toContain('暂时不可用')
    expect(describeError('something_new')).toBe(describeError('internal'))
    expect(describeError(null)).toBe(describeError('internal'))
  })
})
