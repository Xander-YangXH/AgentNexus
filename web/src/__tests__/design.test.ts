import { describe, expect, it } from 'vitest'
import type { Conversation } from '../api/types'
import { conversationLabel, groupByRecency, matches } from '../composables/conversations'
import { PALETTES } from '../theme/tokens'
import { readPreference, resolveMode } from '../theme/useTheme'

const conv = (id: string, last: string, title: string | null = id): Conversation =>
  ({ id, title, created_at: '2026-10-01T00:00:00Z', last_activity_at: last, archived_at: null, agent: 'chat' })

describe('conversation list (decisions/0024)', () => {
  const now = new Date(2026, 9, 24, 15, 0) // 本地时间 10 月 24 日 15:00

  it('groups by recency and keeps the given order inside a group', () => {
    const list = [
      conv('a', new Date(2026, 9, 24, 9).toISOString()),
      conv('b', new Date(2026, 9, 24, 1).toISOString()),
      conv('c', new Date(2026, 9, 23, 22).toISOString()),
      conv('d', new Date(2026, 9, 19).toISOString()),
      conv('e', new Date(2026, 8, 1).toISOString()),
    ]
    expect(groupByRecency(list, now).map((g) => [g.label, g.items.map((c) => c.id)])).toEqual([
      ['今天', ['a', 'b']],
      ['昨天', ['c']],
      ['最近 7 天', ['d']],
      ['更早', ['e']],
    ])
  })

  it('drops empty groups', () => {
    expect(groupByRecency([conv('a', new Date(2026, 8, 1).toISOString())], now).map((g) => g.label)).toEqual(['更早'])
  })

  it('searches titles case-insensitively and falls back to the date for untitled ones', () => {
    expect(matches(conv('x', '', 'Redis 缓存'), 'redis')).toBe(true)
    expect(matches(conv('x', '', 'Redis 缓存'), 'kafka')).toBe(false)
    expect(conversationLabel(conv('x', '', null))).toMatch(/^对话 · /)
  })
})

describe('theme (decisions/0024)', () => {
  it('resolves the system preference', () => {
    expect([resolveMode('system', true), resolveMode('system', false), resolveMode('light', true)]).toEqual(['dark', 'light', 'light'])
  })

  it('reads a stored preference and ignores garbage or a broken storage', () => {
    expect(readPreference({ getItem: () => 'dark' })).toBe('dark')
    expect(readPreference({ getItem: () => 'purple' })).toBe('system')
    expect(readPreference({ getItem: () => { throw new Error('denied') } })).toBe('system') // 隐私模式下 localStorage 可能抛错
    expect(readPreference(null)).toBe('system')
  })

  it('defines every token in both modes', () => {
    expect(Object.keys(PALETTES.dark).sort()).toEqual(Object.keys(PALETTES.light).sort())
  })
})
