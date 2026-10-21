/**
 * 消息分页的合并（纯函数，便于单测）。
 *
 * 刷新只会拉取“最近一页”。如果直接用它覆盖，用户之前点“加载更早”翻出来的消息就会消失；
 * 所以按 seq 合并、去重、排序。同一条消息以新拉到的为准（例如以后支持编辑时内容可能变化）。
 */
import type { Message } from '../api/types'

export function mergeMessages(existing: Message[], incoming: Message[]): Message[] {
  const bySeq = new Map<number, Message>()
  for (const m of existing) bySeq.set(m.seq, m)
  for (const m of incoming) bySeq.set(m.seq, m)
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq)
}
