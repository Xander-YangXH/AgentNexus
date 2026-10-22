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
 *
 * 知识库检索（decisions/0018）：tool.started 时清空已显示的文字——模型在调用工具前说的话不会成为最终回复
 * （最终回复取自图的最终状态），并显示“正在检索”；tool.finished 后回到“等待输出”，直到回答的第一个字。
 *
 * 接管（decisions/0020）：执行它的 worker 中断后，别的 worker 从 checkpoint 接着做，先发 run.resumed {attempt}。
 * 这时清空已显示的半截文字（新的执行会重新流出被打断的那一步）；之后只接受这一次尝试的事件——
 * 旧 worker 在心跳发现自己被接管之前，可能还会流出几个 token，它们带着更小的 attempt，直接忽略。
 */
import type { RunEvent } from '../api/types'

export type PendingStatus = 'waiting' | 'streaming' | 'completed' | 'failed' | 'cancelled'

export interface PendingReply {
  runId: string
  status: PendingStatus
  text: string
  errorCode: string | null
  startedAt: number // 用于显示“等待首字 N 秒”，取服务端的 run 创建时间
  activity: string | null // 正在做什么（如“正在检索：退货政策”），等待阶段显示
  attempt: number // 当前接受的是第几次执行的事件（被接管后递增）
}

export function newPending(runId: string, now: number = Date.now()): PendingReply {
  return { runId, status: 'waiting', text: '', errorCode: null, startedAt: now, activity: null, attempt: 1 }
}

export function isActive(p: PendingReply | null): boolean {
  return p?.status === 'waiting' || p?.status === 'streaming'
}

export function applyRunEvent(p: PendingReply, e: RunEvent): PendingReply {
  if (!isActive(p)) return p // 终态之后的事件忽略
  // 第一次执行的事件不带 attempt（后端只在接管后才加），当作 1。第一版写成“有 attempt 才比较”，
  // 第一次执行的残余 token 就漏过去了（补测试时发现）
  const attempt = (e.data as { attempt?: number }).attempt ?? 1
  if (e.type !== 'run.resumed' && attempt < p.attempt) return p // 被接管前那次执行的残余事件
  switch (e.type) {
    case 'run.resumed':
      if (e.data.attempt <= p.attempt) return p
      return { ...p, status: 'waiting', text: '', attempt: e.data.attempt, activity: '执行中断，已由其他进程接着生成' }
    case 'run.started':
      return p
    case 'token':
      return { ...p, status: 'streaming', text: p.text + e.data.text, activity: null }
    case 'tool.started':
      return { ...p, status: 'waiting', text: '', activity: `正在检索知识库：${e.data.query}` }
    case 'tool.finished':
      return { ...p, activity: e.data.ok ? `检索到 ${e.data.hits} 条资料，正在生成回答` : '知识库检索失败，正在生成回答' }
    case 'run.completed':
      return { ...p, status: 'completed' }
    case 'run.failed':
      return { ...p, status: 'failed', errorCode: e.data.error_code ?? 'internal' }
    case 'run.cancelled':
      return { ...p, status: 'cancelled' }
    default:
      // 不认识的事件（后端新增了类型、前端还是旧版本）忽略。第一版没有 default，会返回 undefined 把状态弄坏
      return p
  }
}
