import type { Conversation, Message, Run, User } from './types'

export class HttpError extends Error {
  constructor(
    public status: number,
    public detail: unknown,
  ) {
    super(`HTTP ${status}`)
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
  const data = text ? JSON.parse(text) : null
  if (!res.ok) throw new HttpError(res.status, data?.detail ?? data)
  return data as T
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
