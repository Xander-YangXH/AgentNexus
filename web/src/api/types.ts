// 与后端 server/schemas.py 对应的类型

export interface User {
  id: string
  name: string
}

export interface Conversation {
  id: string
  title: string | null
  created_at: string
}

export interface Message {
  id: string
  role: 'user' | 'assistant' | 'system' | 'tool'
  content: string
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
  | { type: 'token'; data: { text: string }; id: string | null }
  | { type: 'run.completed'; data: { message_id: string; synthesized?: boolean }; id: string | null }
  | { type: 'run.failed'; data: { error_code: string | null; synthesized?: boolean }; id: string | null }
  | { type: 'run.cancelled'; data: { synthesized?: boolean }; id: string | null }

export const TERMINAL_EVENTS = new Set(['run.completed', 'run.failed', 'run.cancelled'])
