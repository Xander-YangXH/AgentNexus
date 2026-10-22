import type {
  AgentKind,
  Attachment,
  Chunk,
  Conversation,
  EntityDetail,
  GraphBuild,
  GraphData,
  GraphEstimate,
  GraphNode,
  KnowledgeBase,
  KnowledgeDocument,
  Message,
  Run,
  SearchResult,
  User,
} from './types'

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

async function send(method: string, path: string, body?: unknown): Promise<{ data: unknown; res: Response }> {
  // 相对路径：开发时由 Vite 代理、部署时由 Nginx 转发到后端，浏览器只看到一个源，不需要 CORS
  const res = await fetch(path, {
    method,
    headers: { ...authHeaders(), ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const text = await res.text()
  if (!res.ok) throw new HttpError(res.status, parseErrorBody(text))
  return { data: text ? JSON.parse(text) : null, res }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  return (await send(method, path, body)).data as T
}

/** 上传文件：multipart/form-data。不手动设置 Content-Type，让浏览器带上 boundary */
async function upload<T>(path: string, file: File): Promise<T> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(path, { method: 'POST', headers: authHeaders(), body: form })
  const text = await res.text()
  if (!res.ok) throw new HttpError(res.status, parseErrorBody(text))
  return JSON.parse(text) as T
}

/**
 * 下载产物（decisions/0022）。接口要带 X-User-Id 头，普通 <a href> 带不上，所以先 fetch 成 Blob，
 * 再用临时的对象 URL 触发下载。文件名用接口记录里的（服务端也在 Content-Disposition 里给了）。
 */
async function downloadArtifact(a: Attachment): Promise<void> {
  const res = await fetch(`/api/artifacts/${a.id}/download`, { headers: authHeaders() })
  if (!res.ok) throw new HttpError(res.status, parseErrorBody(await res.text()))
  const url = URL.createObjectURL(await res.blob())
  try {
    const link = document.createElement('a')
    link.href = url
    link.download = a.filename
    link.click()
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
}

export interface MessagePage {
  items: Message[]
  hasMore: boolean // 来自响应头 X-Has-More
}

async function listMessagesPage(cid: string, beforeSeq?: number, limit = 50): Promise<MessagePage> {
  const q = new URLSearchParams({ limit: String(limit) })
  if (beforeSeq !== undefined) q.set('before_seq', String(beforeSeq))
  const { data, res } = await send('GET', `/api/conversations/${cid}/messages?${q}`)
  return { items: data as Message[], hasMore: res.headers.get('X-Has-More') === 'true' }
}

export const api = {
  listUsers: () => request<User[]>('GET', '/api/users'),
  createUser: (name: string) => request<User>('POST', '/api/users', { name }),
  listConversations: (archived = false) =>
    request<Conversation[]>('GET', `/api/conversations${archived ? '?archived=true' : ''}`),
  updateConversation: (cid: string, patch: { title?: string | null; archived?: boolean }) =>
    request<Conversation>('PATCH', `/api/conversations/${cid}`, patch),
  createConversation: (title: string | null, agent: AgentKind = 'chat') =>
    request<Conversation>('POST', '/api/conversations', { title, agent }),
  listMessagesPage,
  sendMessage: (cid: string, content: string) => request<Run>('POST', `/api/conversations/${cid}/messages`, { content }),
  activeRun: (cid: string) => request<Run | null>('GET', `/api/conversations/${cid}/active-run`),
  listRuns: (cid: string) => request<Run[]>('GET', `/api/conversations/${cid}/runs`),
  cancelRun: (runId: string) => request<Run>('POST', `/api/runs/${runId}/cancel`),
  retryRun: (runId: string) => request<Run>('POST', `/api/runs/${runId}/retry`),
  answerApproval: (runId: string, approved: boolean, feedback?: string) =>
    request<Run>('POST', `/api/runs/${runId}/approval`, { approved, feedback: feedback ?? null }),
  downloadArtifact,

  getConversationKnowledgeBases: (cid: string) =>
    request<{ knowledge_base_ids: string[] }>('GET', `/api/conversations/${cid}/knowledge-bases`),
  setConversationKnowledgeBases: (cid: string, ids: string[]) =>
    request<{ knowledge_base_ids: string[] }>('PUT', `/api/conversations/${cid}/knowledge-bases`, { knowledge_base_ids: ids }),
  listKnowledgeBases: () => request<KnowledgeBase[]>('GET', '/api/knowledge-bases'),
  createKnowledgeBase: (name: string) => request<KnowledgeBase>('POST', '/api/knowledge-bases', { name }),
  listDocuments: (kbId: string) => request<KnowledgeDocument[]>('GET', `/api/knowledge-bases/${kbId}/documents`),
  uploadDocument: (kbId: string, file: File) => upload<KnowledgeDocument>(`/api/knowledge-bases/${kbId}/documents`, file),
  reprocessDocument: (docId: string) => request<KnowledgeDocument>('POST', `/api/documents/${docId}/reprocess`),
  listChunks: (docId: string) => request<Chunk[]>('GET', `/api/documents/${docId}/chunks`),
  graphEstimate: (kbId: string) => request<GraphEstimate>('GET', `/api/knowledge-bases/${kbId}/graph/estimate`),
  createGraphBuild: (kbId: string) => request<GraphBuild>('POST', `/api/knowledge-bases/${kbId}/graph/builds`),
  listGraphBuilds: (kbId: string) => request<GraphBuild[]>('GET', `/api/knowledge-bases/${kbId}/graph/builds`),
  graphOverview: (kbId: string, limit = 60) => request<GraphData>('GET', `/api/knowledge-bases/${kbId}/graph/overview?limit=${limit}`),
  graphSearch: (kbId: string, q: string) =>
    request<GraphNode[]>('GET', `/api/knowledge-bases/${kbId}/graph/search?q=${encodeURIComponent(q)}`),
  graphPath: (kbId: string, source: string, target: string) =>
    request<GraphData | null>('GET', `/api/knowledge-bases/${kbId}/graph/path?source=${source}&target=${target}`),
  entityDetail: (id: string) => request<EntityDetail>('GET', `/api/graph/entities/${id}`),
  entityNeighbors: (id: string, limit = 40) => request<GraphData>('GET', `/api/graph/entities/${id}/neighbors?limit=${limit}`),
  search: (kbIds: string[], query: string, topK: number, rerank: boolean) =>
    request<SearchResult>('POST', '/api/knowledge/search', { knowledge_base_ids: kbIds, query, top_k: topK, rerank }),
}
