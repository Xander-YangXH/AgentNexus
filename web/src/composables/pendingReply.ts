/**
 * “正在生成的那条回复”的状态机（纯函数，便于单测）。
 *
 *   waiting ──token──▶ streaming ──run.completed──▶ completed
 *      │                   │
 *      └──run.failed───────┴──▶ failed（保留已收到的半截文本，界面标成“生成中断”）
 *
 * 为什么 completed 之后不直接用拼好的文本当最终结果：数据库才是事实来源（decisions/0006/0007），
 * 完成后重新拉一次消息列表，用库里的助手消息替换掉这条临时回复。
 */
import type { RunEvent } from '../api/types'

export type PendingStatus = 'waiting' | 'streaming' | 'completed' | 'failed'

export interface PendingReply {
  runId: string
  status: PendingStatus
  text: string
  error: string | null
  startedAt: number // 用于显示“等待首字 N 秒”
}

export function newPending(runId: string, now: number = Date.now()): PendingReply {
  return { runId, status: 'waiting', text: '', error: null, startedAt: now }
}

export function applyRunEvent(p: PendingReply, e: RunEvent): PendingReply {
  if (p.status === 'completed' || p.status === 'failed') return p // 终态之后的事件忽略
  switch (e.type) {
    case 'run.started':
      return p
    case 'token':
      return { ...p, status: 'streaming', text: p.text + e.data.text }
    case 'run.completed':
      return { ...p, status: 'completed' }
    case 'run.failed':
      return { ...p, status: 'failed', error: e.data.error ?? '未知错误' }
    case 'run.cancelled':
      return { ...p, status: 'failed', error: '已取消' }
  }
}
