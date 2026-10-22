// 与后端 server/schemas.py 对应的类型

export interface User {
  id: string
  name: string
}

export interface Conversation {
  id: string
  title: string | null
  created_at: string
  last_activity_at: string
  archived_at: string | null
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
  created_at: string
}

export type RunStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'

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
}

// 事件契约见后端 agentnexus/events/types.py 与 decisions/0007
export type RunEvent =
  | { type: 'run.started'; data: Record<string, never>; id: string | null }
  | { type: 'token'; data: { text: string; attempt?: number }; id: string | null }
  | { type: 'run.completed'; data: { message_id: string; synthesized?: boolean }; id: string | null }
  | { type: 'run.failed'; data: { error_code: string | null; synthesized?: boolean }; id: string | null }
  | { type: 'run.cancelled'; data: { synthesized?: boolean }; id: string | null }
  | { type: 'run.resumed'; data: { attempt: number }; id: string | null }
  | { type: 'tool.started'; data: { name: string; query: string; attempt?: number }; id: string | null }
  | { type: 'tool.finished'; data: { name: string; hits: number; ok: boolean; attempt?: number }; id: string | null }

export const TERMINAL_EVENTS = new Set(['run.completed', 'run.failed', 'run.cancelled'])

// ---------- 知识库（decisions/0015、0016、0017） ----------

export interface KnowledgeBase {
  id: string
  name: string
  description: string | null
  created_at: string
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
