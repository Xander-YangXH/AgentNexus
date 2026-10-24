// 对话列表按最近活动分组（decisions/0024），纯函数便于单测
import type { Conversation } from '../api/types'

export interface ConversationGroup {
  label: string
  items: Conversation[]
}

const DAY = 24 * 60 * 60 * 1000

export function groupByRecency(conversations: Conversation[], now: Date = new Date()): ConversationGroup[] {
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const buckets: [string, (t: number) => boolean][] = [
    ['今天', (t) => t >= startOfToday],
    ['昨天', (t) => t >= startOfToday - DAY],
    ['最近 7 天', (t) => t >= startOfToday - 7 * DAY],
    ['更早', () => true],
  ]
  const groups: ConversationGroup[] = buckets.map(([label]) => ({ label, items: [] }))
  for (const c of conversations) {
    const t = Date.parse(c.last_activity_at)
    groups[buckets.findIndex(([, match]) => match(t))].items.push(c)
  }
  return groups.filter((g) => g.items.length)
}

export function conversationLabel(c: Conversation): string {
  return c.title || `对话 · ${new Date(c.created_at).toLocaleDateString()}`
}

export function matches(c: Conversation, query: string): boolean {
  const q = query.trim().toLowerCase()
  return !q || conversationLabel(c).toLowerCase().includes(q)
}
