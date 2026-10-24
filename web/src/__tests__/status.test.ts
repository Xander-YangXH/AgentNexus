import { describe, expect, it } from 'vitest'
import type { SystemStatus } from '../api/types'
import { formatDuration, statusItems } from '../composables/status'

const base: SystemStatus = {
  runs: { queued: 0, running: 1, awaiting_approval: 0, oldest_queued_seconds: 0, succeeded_24h: 12, failed_24h: 0,
          p50_seconds_24h: 2.4, p95_seconds_24h: 8 },
  documents: { pending: 0, processing: 0, failed: 0, oldest_pending_seconds: 0 },
  graph_builds: { pending: 0, running: 0 },
  purge_jobs: { pending: 0, retrying: 0, max_attempts: 0, oldest_seconds: 0 },
  busy_workers: 1,
}

describe('system status', () => {
  it('is quiet when nothing needs attention', () => {
    expect(statusItems(base).some((i) => i.warn)).toBe(false)
  })

  it('flags a stuck queue, failures and retrying cleanup', () => {
    const items = statusItems({
      ...base,
      runs: { ...base.runs, queued: 3, oldest_queued_seconds: 400, failed_24h: 2 },
      documents: { ...base.documents, failed: 1 },
      purge_jobs: { pending: 1, retrying: 1, max_attempts: 4, oldest_seconds: 900 },
    })
    const warned = items.filter((i) => i.warn).map((i) => i.label)
    expect(warned).toEqual(['排队中的回复', '24 小时完成 / 失败', '处理失败的文档', '删除后的清理任务'])
    expect(items[0].hint).toBe('最久的已等 7 分钟')
    expect(items.find((i) => i.label === '删除后的清理任务')?.hint).toBe('1 个在重试（最多 4 次）')
  })

  it('formats durations and missing quantiles', () => {
    expect(formatDuration(null)).toBe('—')
    expect(formatDuration(42)).toBe('42 秒')
    expect(formatDuration(5400)).toBe('1.5 小时')
  })
})
