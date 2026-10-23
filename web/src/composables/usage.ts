// 用量看板的纯函数（decisions/0023），便于单测
import type { Me, Role, UsageRow, UsageScope } from '../api/types'

export const ROLE_LABEL: Record<Role, string> = { admin: '管理员', manager: '部门负责人', member: '成员' }

/** 角色能看的范围（与后端 UsageService.scope_filter 一致；后端同样检查） */
export function scopesFor(me: Me): { value: UsageScope; label: string }[] {
  const out: { value: UsageScope; label: string }[] = [{ value: 'me', label: '我自己' }]
  if (me.role === 'admin' || (me.role === 'manager' && me.department_id)) out.push({ value: 'department', label: '部门' })
  if (me.role === 'admin') out.push({ value: 'all', label: '全站' })
  return out
}

const EMPTY: UsageRow = { runs: 0, llm_calls: 0, searches: 0, input_tokens: 0, cached_tokens: 0, output_tokens: 0, cost_usd: 0 }

/** 后端只返回有数据的天；画图要补齐没有数据的天（否则折线会把相隔几天的点直接连起来） */
export function fillDays<T extends UsageRow & { day: string }>(series: T[], days: number, today = new Date()): (UsageRow & { day: string })[] {
  const byDay = new Map(series.map((r) => [r.day, r]))
  const out: (UsageRow & { day: string })[] = []
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate() - i))
    const key = d.toISOString().slice(0, 10)
    out.push({ ...(byDay.get(key) ?? EMPTY), day: key })
  }
  return out
}

export function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}
