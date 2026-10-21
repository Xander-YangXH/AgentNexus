/**
 * 根据 run 列表，算出哪些用户消息下面要标注“回复失败 / 已停止”，以及能不能重试（纯函数，便于单测）。
 *
 * 一条用户消息可能有多个 run（失败后重试）：只看最新的那个。
 * 只有对话的最后一条消息可以重试——后端也有同样的限制（decisions/0009）。
 */
import type { Message, Run } from '../api/types'

export interface RunNotice {
  kind: 'failed' | 'cancelled'
  errorCode: string | null
  runId: string
  retryable: boolean
}

export function computeNotices(messages: Message[], runs: Run[]): Map<string, RunNotice> {
  const latest = new Map<string, Run>()
  for (const r of runs) {
    const prev = latest.get(r.input_message_id)
    if (!prev || r.created_at > prev.created_at) latest.set(r.input_message_id, r)
  }
  const lastId = messages.length ? messages[messages.length - 1].id : null
  const notices = new Map<string, RunNotice>()
  for (const m of messages) {
    if (m.role !== 'user') continue
    const run = latest.get(m.id)
    if (!run || (run.status !== 'failed' && run.status !== 'cancelled')) continue
    notices.set(m.id, {
      kind: run.status,
      errorCode: run.error_code,
      runId: run.id,
      retryable: m.id === lastId,
    })
  }
  return notices
}
