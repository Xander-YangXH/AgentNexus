/**
 * “正在生成的那条回复”的状态机（纯函数，便于单测）。
 *
 *   waiting ──token──▶ streaming ──run.completed──▶ completed
 *      │                   │
 *      ├──run.failed───────┼──▶ failed（保留已收到的半截文本）
 *      └──run.cancelled────┴──▶ cancelled（用户点了“停止”，同样保留半截文本）
 *
 * 为什么 completed 之后不直接用拼好的文本当最终结果：数据库才是事实来源（decisions/0006/0007），
 * 完成后重新拉一次消息列表，用库里的助手消息替换掉这条临时回复。
 * 失败和取消的半截文本都不入库（decisions/0007/0009），只在当前页面上显示。
 */
import type { RunEvent } from '../api/types'

export type PendingStatus = 'waiting' | 'streaming' | 'completed' | 'failed' | 'cancelled'

export interface PendingReply {
  runId: string
  status: PendingStatus
  text: string
  errorCode: string | null
  startedAt: number // 用于显示“等待首字 N 秒”，取服务端的 run 创建时间
}

export function newPending(runId: string, now: number = Date.now()): PendingReply {
  return { runId, status: 'waiting', text: '', errorCode: null, startedAt: now }
}

export function isActive(p: PendingReply | null): boolean {
  return p?.status === 'waiting' || p?.status === 'streaming'
}

export function applyRunEvent(p: PendingReply, e: RunEvent): PendingReply {
  if (!isActive(p)) return p // 终态之后的事件忽略
  switch (e.type) {
    case 'run.started':
      return p
    case 'token':
      return { ...p, status: 'streaming', text: p.text + e.data.text }
    case 'run.completed':
      return { ...p, status: 'completed' }
    case 'run.failed':
      return { ...p, status: 'failed', errorCode: e.data.error_code ?? 'internal' }
    case 'run.cancelled':
      return { ...p, status: 'cancelled' }
  }
}
