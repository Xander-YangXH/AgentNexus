import type { Conversation, Message, Run, User } from './types'

// 后端统一错误格式（decisions/0011）：{"error": {"code", "message", "request_id", "details"?}}
export interface ApiErrorBody {
  code: string
  message: string
  request_id: string | null
  details?: unknown
}

export class HttpError extends Error {
  constructor(
    public status: number,
    public body: ApiErrorBody | null,
  ) {
    super(body ? `${body.code}: ${body.message}` : `HTTP ${status}`)
  }

  get code(): string | null {
    return this.body?.code ?? null
  }

  /** 给用户看的说明：服务端错误时附上请求编号，报告问题时提供它就能在日志里定位 */
  describe(): string {
    if (this.status >= 500 && this.body?.request_id) return `服务出错了（请求编号 ${this.body.request_id}）`
    return this.body?.message ?? `HTTP ${this.status}`
  }
}

/** 解析错误响应体；不是预期格式（例如代理返回的 HTML 错误页）时返回 null */
export function parseErrorBody(text: string): ApiErrorBody | null {
  try {
    const data = JSON.parse(text)
    return data && typeof data.error === 'object' ? (data.error as ApiErrorBody) : null
  } catch {
    return null
  }
}

// 鉴权占位：阶段 5 前用 X-User-Id 头标识用户（见后端 server/deps.py）
let currentUserId: string | null = null
export function setCurrentUser(id: string | null) {
  currentUserId = id
}
export function authHeaders(): Record<string, string> {
  return currentUserId ? { 'X-User-Id': currentUserId } : {}
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  // 相对路径：开发时由 Vite 代理、部署时由 Nginx 转发到后端，浏览器只看到一个源，不需要 CORS
  const res = await fetch(path, {
    method,
    headers: { ...authHeaders(), ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const text = await res.text()
  if (!res.ok) throw new HttpError(res.status, parseErrorBody(text))
  return (text ? JSON.parse(text) : null) as T
}

export const api = {
  listUsers: () => request<User[]>('GET', '/api/users'),
  createUser: (name: string) => request<User>('POST', '/api/users', { name }),
  listConversations: () => request<Conversation[]>('GET', '/api/conversations'),
  createConversation: (title: string | null) => request<Conversation>('POST', '/api/conversations', { title }),
  listMessages: (cid: string) => request<Message[]>('GET', `/api/conversations/${cid}/messages`),
  sendMessage: (cid: string, content: string) => request<Run>('POST', `/api/conversations/${cid}/messages`, { content }),
  activeRun: (cid: string) => request<Run | null>('GET', `/api/conversations/${cid}/active-run`),
  listRuns: (cid: string) => request<Run[]>('GET', `/api/conversations/${cid}/runs`),
  cancelRun: (runId: string) => request<Run>('POST', `/api/runs/${runId}/cancel`),
  retryRun: (runId: string) => request<Run>('POST', `/api/runs/${runId}/retry`),
}
