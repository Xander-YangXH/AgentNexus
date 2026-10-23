// 与后端 server/schemas.py 对应的类型

export type Role = 'admin' | 'manager' | 'member'

/** 当前登录用户（decisions/0023） */
export interface Me {
  id: string
  name: string
  role: Role
  department_id: string | null
  department_name: string | null
}

export interface User {
  id: string
  name: string
  role: Role
  department_id: string | null
  disabled_at: string | null
}

export interface Department {
  id: string
  name: string
}

export type UsageScope = 'me' | 'department' | 'all'

export interface UsageRow {
  runs: number
  llm_calls: number
  searches: number
  input_tokens: number
  cached_tokens: number
  output_tokens: number
  cost_usd: number
}

export interface UsageSummary {
  scope: UsageScope
  days: number
  series: (UsageRow & { day: string })[]
  totals: UsageRow
  by_user: (UsageRow & { user_id: string; name: string })[]
}

export interface Conversation {
  id: string
  title: string | null
  created_at: string
  last_activity_at: string
  archived_at: string | null
  agent: AgentKind
}

/** 对话的 Agent 类型（decisions/0022）：建对话时选定 */
export type AgentKind = 'chat' | 'research'

/** 消息附带的产物（decisions/0022），下载走 /api/artifacts/{id}/download */
export interface Attachment {
  id: string
  filename: string
  content_type: string
  size: number
}

/** 调研计划审批（decisions/0022） */
export interface ResearchPlanRequest {
  kind: 'research_plan'
  request: string
  plan: { title: string; question: string }[]
  estimate: { subtasks: number; model_calls: string }
}

/** 助手回答引用的知识库片段快照（decisions/0018）。n 对应正文里的 [n]；cited=false 表示检索到但回答没用上 */
export interface Citation {
  n: number
  chunk_id: string
  document_id: string
  knowledge_base_id: string
  filename: string
  page: number | null
  seq: number
  text: string
  vector_score: number
  rerank_score: number | null
  cited: boolean
}

export interface Message {
  id: string
  seq: number // 分页游标
  role: 'user' | 'assistant' | 'system' | 'tool'
  content: string
  citations?: Citation[] | null
  attachments?: Attachment[] | null
  created_at: string
}

export type RunStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'awaiting_approval'

export interface Run {
  id: string
  conversation_id: string
  input_message_id: string
  output_message_id: string | null
  status: RunStatus
  error_code: string | null // 原始报错不对外，只有错误码（decisions/0009）
  created_at: string
  started_at: string | null
  finished_at: string | null
  approval_request?: ResearchPlanRequest | null // 等待审批时要展示的内容
}

// 事件契约见后端 agentnexus/events/types.py 与 decisions/0007
export type RunEvent =
  | { type: 'run.started'; data: Record<string, never>; id: string | null }
  | { type: 'token'; data: { text: string; attempt?: number }; id: string | null }
  | { type: 'run.completed'; data: { message_id: string; synthesized?: boolean }; id: string | null }
  | { type: 'run.failed'; data: { error_code: string | null; synthesized?: boolean }; id: string | null }
  | { type: 'run.cancelled'; data: { synthesized?: boolean }; id: string | null }
  | { type: 'run.resumed'; data: { attempt: number }; id: string | null }
  // detail：参数摘要（检索词、网址、文件路径、技能名或代码第一行），decisions/0021 起所有工具都有
  | { type: 'tool.started'; data: { name: string; query: string; detail?: string; subtask?: number; attempt?: number }; id: string | null }
  | { type: 'tool.finished'; data: { name: string; hits: number; ok: boolean; subtask?: number; attempt?: number }; id: string | null }
  // 等待审批（decisions/0022）：服务端在这里结束事件流；答复后带 Last-Event-ID 重新订阅
  | { type: 'run.awaiting_approval'; data: { request: ResearchPlanRequest; synthesized?: boolean }; id: string | null }
  | { type: 'research.subtask'; data: { index: number; title: string; status: 'started' | 'finished' | 'failed'; total: number }; id: string | null }

export const TERMINAL_EVENTS = new Set(['run.completed', 'run.failed', 'run.cancelled'])
// 事件流在这些事件处结束，但 run 还没结束
export const PAUSE_EVENTS = new Set(['run.awaiting_approval'])

// ---------- 知识库（decisions/0015、0016、0017） ----------

export interface KnowledgeBase {
  id: string
  name: string
  description: string | null
  created_at: string
  user_id: string // 所有者；不是自己 = 部门共享给我的，只读（decisions/0023）
  visibility: 'private' | 'department'
  department_id: string | null
}

export type DocumentStatus = 'pending' | 'processing' | 'ready' | 'failed'

export interface KnowledgeDocument {
  id: string
  knowledge_base_id: string
  filename: string
  size_bytes: number
  status: DocumentStatus
  error_code: string | null // 原始报错不对外
  chunk_count: number
  created_at: string
  processed_at: string | null
}

export interface Chunk {
  id: string
  seq: number
  text: string
  token_count: number
  page: number | null
}

export interface SearchHit {
  chunk_id: string
  document_id: string
  knowledge_base_id: string
  filename: string
  seq: number
  page: number | null
  text: string
  vector_score: number // 余弦相似度
  rerank_score: number | null // 重排模型原始分数，只在同一个问题内部可比
}

export interface SearchResult {
  hits: SearchHit[]
  reranked: boolean
  timings_ms: Record<string, number>
}

// ---------- 知识图谱（decisions/0019） ----------

export interface GraphEstimate {
  chunk_count: number
  unique_count: number
  cached_count: number
  extract_count: number
  estimated_cost_usd: number
  max_extract_count: number
}

export type GraphBuildStatus = 'pending' | 'running' | 'succeeded' | 'failed'

export interface GraphBuild {
  id: string
  status: GraphBuildStatus
  chunk_count: number
  unique_count: number
  extract_count: number
  estimated_cost_usd: number
  done_count: number
  input_tokens: number
  cached_tokens: number
  output_tokens: number
  cost_usd: number
  entity_count: number
  relation_count: number
  error_code: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface GraphNode {
  id: string
  name: string
  type: string
  mentions: number
}

export interface GraphEdge {
  id: string
  source: string
  target: string
  predicate: string
  weight: number
}

export interface GraphData {
  nodes: GraphNode[]
  edges: GraphEdge[]
}

export interface EntityDetail {
  id: string
  knowledge_base_id: string
  name: string
  type: string
  description: string
  mention_count: number
  relations: { id: string; direction: 'in' | 'out'; predicate: string; description: string; other_id: string; other_name: string; mentions: number }[]
  mentions: { chunk_id: string; filename: string; page: number | null; seq: number; text: string }[]
}
