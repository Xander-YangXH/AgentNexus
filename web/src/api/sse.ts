/**
 * 基于 fetch 的 SSE 客户端。
 *
 * 为什么不用浏览器原生 EventSource：它不能设置自定义请求头，而我们用 X-User-Id 头鉴权（阶段 5 换成 token 也一样在头里）。
 * 也不把用户标识放进 URL 查询串：URL 会进入访问日志和浏览器历史。
 *
 * 分两部分：
 * - SseParser：按 WHATWG 规范把字节流解析成事件。纯逻辑、可单测；能处理任意位置被切断的数据块。
 * - streamRunEvents：负责连接、断线重连（带 Last-Event-ID 续传）、收到终态事件后结束。
 */
import { HttpError, parseErrorBody } from './http'
import { TERMINAL_EVENTS, type RunEvent } from './types'

export interface SseMessage {
  event: string
  data: string
  id: string | null
}

export class SseParser {
  private buffer = ''
  private pendingCR = false
  private data: string[] = []
  private eventType = ''
  private lastEventId: string | null = null
  private decoder = new TextDecoder()

  /** 喂入原始字节。多字节字符（如中文）可能被切在两个块之间，TextDecoder 的 stream 模式会把残缺部分留到下一块。 */
  feedBytes(chunk: Uint8Array): SseMessage[] {
    return this.feed(this.decoder.decode(chunk, { stream: true }))
  }

  feed(text: string): SseMessage[] {
    // 上一块以 \r 结尾、这一块以 \n 开头：这是被切开的 \r\n，\n 已经算在上一行里了，跳过
    if (text === '') return [] // 空块（网络层偶尔会交付）不能清掉下面的 pendingCR 标记
    if (this.pendingCR && text.startsWith('\n')) text = text.slice(1)
    this.pendingCR = false
    this.buffer += text
    const out: SseMessage[] = []
    // 行结束符可以是 \r\n、\r 或 \n
    for (;;) {
      const match = /\r\n|\r|\n/.exec(this.buffer)
      if (!match) break
      // 遇到 \r 立即结束这一行，不能等下一块：只用 \r 换行的流里，最后一个 \r 之后可能很久都没有数据
      if (match[0] === '\r' && match.index === this.buffer.length - 1) this.pendingCR = true
      const line = this.buffer.slice(0, match.index)
      this.buffer = this.buffer.slice(match.index + match[0].length)
      const msg = this.processLine(line)
      if (msg) out.push(msg)
    }
    return out
  }

  private processLine(line: string): SseMessage | null {
    if (line === '') return this.dispatch()
    if (line.startsWith(':')) return null // 注释，例如服务端的 ": ping" 保活
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'data') this.data.push(value)
    else if (field === 'event') this.eventType = value
    else if (field === 'id' && !value.includes('\0')) this.lastEventId = value
    // retry 与未知字段忽略
    return null
  }

  private dispatch(): SseMessage | null {
    const type = this.eventType || 'message'
    this.eventType = ''
    if (this.data.length === 0) return null
    const msg = { event: type, data: this.data.join('\n'), id: this.lastEventId }
    this.data = []
    return msg
  }
}

export interface StreamOptions {
  headers: Record<string, string>
  onEvent: (event: RunEvent) => void
  signal?: AbortSignal
  maxRetries?: number
  retryDelayMs?: number
  fetchImpl?: typeof fetch // 测试注入
}

const sleep = (ms: number, signal?: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    const t = setTimeout(resolve, ms)
    signal?.addEventListener('abort', () => {
      clearTimeout(t)
      reject(signal.reason ?? new DOMException('Aborted', 'AbortError'))
    })
  })

/**
 * 订阅一个 run 的事件，直到收到终态事件为止，返回该终态事件。
 * 网络中断或服务端在终态前关闭连接时自动重连，并带上 Last-Event-ID，从断点继续，不重复、不遗漏。
 * 401/404 这类“请求本身错了”的情况不重试。
 */
export async function streamRunEvents(runId: string, opts: StreamOptions): Promise<RunEvent> {
  const doFetch = opts.fetchImpl ?? fetch
  const maxRetries = opts.maxRetries ?? 5
  const retryDelayMs = opts.retryDelayMs ?? 1000
  let lastEventId: string | null = null
  let failures = 0

  for (;;) {
    try {
      const res: Response = await doFetch(`/api/runs/${runId}/events`, {
        headers: {
          ...opts.headers,
          Accept: 'text/event-stream',
          ...(lastEventId ? { 'Last-Event-ID': lastEventId } : {}),
        },
        signal: opts.signal,
      })
      if (res.status >= 400 && res.status < 500) throw new HttpError(res.status, parseErrorBody(await res.text()))
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)

      const parser = new SseParser()
      const reader: ReadableStreamDefaultReader<Uint8Array> = res.body.getReader()
      for (;;) {
        const { value, done }: ReadableStreamReadResult<Uint8Array> = await reader.read()
        if (done) break
        const messages: SseMessage[] = parser.feedBytes(value)
        for (const msg of messages) {
          // 合成的终态事件没有 id，不能作为续传游标，所以只在有 id 时更新
          if (msg.id) lastEventId = msg.id
          const event = { type: msg.event, data: JSON.parse(msg.data), id: msg.id } as RunEvent
          failures = 0 // 收到数据说明连接正常，重置失败计数
          opts.onEvent(event)
          if (TERMINAL_EVENTS.has(event.type)) {
            await reader.cancel()
            return event
          }
        }
      }
      throw new Error('stream closed before a terminal event')
    } catch (err) {
      if (err instanceof HttpError || opts.signal?.aborted) throw err
      failures += 1
      if (failures > maxRetries) throw err
      await sleep(retryDelayMs * failures, opts.signal)
    }
  }
}
