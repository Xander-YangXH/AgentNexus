import { describe, expect, it, vi } from 'vitest'
import { HttpError } from '../api/http'
import { SseParser, streamRunEvents, type SseMessage } from '../api/sse'
import type { RunEvent } from '../api/types'

const enc = new TextEncoder()

function parseAll(chunks: string[]): SseMessage[] {
  const p = new SseParser()
  return chunks.flatMap((c) => p.feed(c))
}

const SAMPLE =
  'event: run.started\ndata: {}\nid: 1-0\n\n' +
  ': ping\n\n' +
  'event: token\ndata: {"text":"你好"}\nid: 1-1\n\n' +
  'event: run.completed\ndata: {"message_id":"m"}\nid: 1-2\n\n'

describe('SseParser', () => {
  it('parses events, ignores comments', () => {
    const msgs = parseAll([SAMPLE])
    expect(msgs.map((m) => [m.event, m.data, m.id])).toEqual([
      ['run.started', '{}', '1-0'],
      ['token', '{"text":"你好"}', '1-1'],
      ['run.completed', '{"message_id":"m"}', '1-2'],
    ])
  })

  it('gives the same result no matter where the stream is cut', () => {
    const whole = parseAll([SAMPLE])
    expect(parseAll(SAMPLE.split(''))).toEqual(whole) // 逐字符喂入
    for (let i = 1; i < SAMPLE.length; i++) {
      expect(parseAll([SAMPLE.slice(0, i), SAMPLE.slice(i)])).toEqual(whole) // 任意位置切成两块
    }
  })

  it('accepts CRLF and CR line endings, including CRLF split across chunks', () => {
    const crlf = SAMPLE.replace(/\n/g, '\r\n')
    expect(parseAll([crlf])).toEqual(parseAll([SAMPLE]))
    expect(parseAll([SAMPLE.replace(/\n/g, '\r')])).toEqual(parseAll([SAMPLE]))
    // \r 在第一块末尾、\n 在第二块开头：不能被当成两个换行（否则会提前派发一个空事件、打乱字段）
    const cut = crlf.indexOf('\r\n') + 1
    expect(parseAll([crlf.slice(0, cut), crlf.slice(cut)])).toEqual(parseAll([SAMPLE]))
    expect(parseAll([crlf.slice(0, cut), '', crlf.slice(cut)])).toEqual(parseAll([SAMPLE])) // 中间夹一个空块
  })

  it('reassembles multi-byte characters split across byte chunks', () => {
    const bytes = enc.encode('event: token\ndata: {"text":"中文"}\n\n')
    const splitAt = bytes.indexOf(0xe4) + 1 // 切在“中”字的第一个字节之后
    const p = new SseParser()
    const msgs = [...p.feedBytes(bytes.slice(0, splitAt)), ...p.feedBytes(bytes.slice(splitAt))]
    expect(JSON.parse(msgs[0].data).text).toBe('中文')
  })

  it('joins multi-line data and does not dispatch events without data', () => {
    expect(parseAll(['data: a\ndata: b\n\n'])[0].data).toBe('a\nb')
    expect(parseAll(['event: x\n\n', 'data: y\n\n'])).toEqual([{ event: 'message', data: 'y', id: null }])
  })
})

// ---------- streamRunEvents ----------

function sseResponse(body: string, status = 200): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      c.enqueue(enc.encode(body))
      c.close()
    },
  })
  return new Response(stream, { status, headers: { 'content-type': 'text/event-stream' } })
}

const ev = (type: string, data: object, id: string) => `event: ${type}\ndata: ${JSON.stringify(data)}\nid: ${id}\n\n`

function headerOf(call: unknown[], name: string): string | undefined {
  const init = call[1] as RequestInit
  return (init.headers as Record<string, string>)[name]
}

describe('streamRunEvents', () => {
  it('delivers events in order and resolves with the terminal event', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      sseResponse(ev('run.started', {}, '1') + ev('token', { text: 'hi' }, '2') + ev('run.completed', { message_id: 'm' }, '3')),
    )
    const seen: RunEvent[] = []
    const terminal = await streamRunEvents('r', { headers: { 'X-User-Id': 'u' }, onEvent: (e) => seen.push(e), fetchImpl })
    expect(seen.map((e) => e.type)).toEqual(['run.started', 'token', 'run.completed'])
    expect(terminal.type).toBe('run.completed')
    expect(headerOf(fetchImpl.mock.calls[0], 'X-User-Id')).toBe('u') // 鉴权头随请求发出
    expect(headerOf(fetchImpl.mock.calls[0], 'Last-Event-ID')).toBeUndefined()
  })

  it('stops at an approval pause and can resume after it (decisions/0022)', async () => {
    const plan = { kind: 'research_plan', request: 'x', plan: [], estimate: { subtasks: 0, model_calls: '2~2' } }
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(sseResponse(ev('run.started', {}, '1') + ev('run.awaiting_approval', { request: plan }, '2')))
      .mockResolvedValueOnce(sseResponse(ev('run.started', {}, '3') + ev('run.completed', { message_id: 'm' }, '4')))
    const paused = await streamRunEvents('r', { headers: {}, onEvent: () => {}, fetchImpl })
    expect(paused.type).toBe('run.awaiting_approval')
    expect(fetchImpl).toHaveBeenCalledTimes(1) // 暂停不是断线，不重连
    const done = await streamRunEvents('r', { headers: {}, onEvent: () => {}, fetchImpl, lastEventId: paused.id })
    expect(done.type).toBe('run.completed')
    expect(headerOf(fetchImpl.mock.calls[1], 'Last-Event-ID')).toBe('2') // 从审批事件之后继续
  })

  it('reconnects with Last-Event-ID when the stream ends before a terminal event', async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(sseResponse(ev('run.started', {}, '1') + ev('token', { text: 'a' }, '2'))) // 中途断开
      .mockResolvedValueOnce(sseResponse(ev('token', { text: 'b' }, '3') + ev('run.completed', { message_id: 'm' }, '4')))
    const seen: RunEvent[] = []
    await streamRunEvents('r', { headers: {}, onEvent: (e) => seen.push(e), fetchImpl, retryDelayMs: 1 })
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(headerOf(fetchImpl.mock.calls[1], 'Last-Event-ID')).toBe('2') // 从断点继续
    expect(seen.map((e) => e.id)).toEqual(['1', '2', '3', '4']) // 不重复、不遗漏
  })

  it('retries after a network error', async () => {
    const fetchImpl = vi
      .fn()
      .mockRejectedValueOnce(new TypeError('network down'))
      .mockResolvedValueOnce(sseResponse(ev('run.completed', { message_id: 'm' }, '1')))
    const t = await streamRunEvents('r', { headers: {}, onEvent: () => {}, fetchImpl, retryDelayMs: 1 })
    expect(t.type).toBe('run.completed')
  })

  it('does not retry client errors such as 404', async () => {
    const body = { error: { code: 'run_not_found', message: 'run not found', request_id: 'r1' } }
    const fetchImpl = vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status: 404 }))
    const err = await streamRunEvents('r', { headers: {}, onEvent: () => {}, fetchImpl, retryDelayMs: 1 }).catch((e) => e)
    expect(err).toBeInstanceOf(HttpError)
    expect(err.code).toBe('run_not_found') // 解析了统一错误格式
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })

  it('gives up after maxRetries consecutive failures', async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new TypeError('network down'))
    await expect(
      streamRunEvents('r', { headers: {}, onEvent: () => {}, fetchImpl, retryDelayMs: 1, maxRetries: 2 }),
    ).rejects.toThrow('network down')
    expect(fetchImpl).toHaveBeenCalledTimes(3) // 首次 + 2 次重试
  })

  it('stops immediately when aborted', async () => {
    const controller = new AbortController()
    const fetchImpl = vi.fn().mockImplementation(async () => {
      controller.abort()
      throw new DOMException('Aborted', 'AbortError')
    })
    await expect(
      streamRunEvents('r', { headers: {}, onEvent: () => {}, fetchImpl, signal: controller.signal, retryDelayMs: 1 }),
    ).rejects.toThrow()
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })
})
