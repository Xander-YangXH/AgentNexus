import type { SystemStatus } from '../api/types'

// 管理员页面的“运行状态”（decisions/0031）：把后端聚合的计数变成卡片上的条目；需要人看一眼的标成警告
export interface StatusItem {
  label: string
  value: string
  warn: boolean
  hint?: string
}

export function formatDuration(seconds: number | null): string {
  if (seconds === null) return '—'
  if (seconds < 60) return `${Math.round(seconds)} 秒`
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟`
  return `${(seconds / 3600).toFixed(1)} 小时`
}

export function statusItems(s: SystemStatus): StatusItem[] {
  const { runs, documents, purge_jobs: purge } = s
  return [
    { label: '排队中的回复', value: String(runs.queued), warn: runs.oldest_queued_seconds > 60,
      hint: runs.queued ? `最久的已等 ${formatDuration(runs.oldest_queued_seconds)}` : undefined },
    { label: '生成中 / 等待审批', value: `${runs.running} / ${runs.awaiting_approval}`, warn: false },
    { label: '24 小时完成 / 失败', value: `${runs.succeeded_24h} / ${runs.failed_24h}`, warn: runs.failed_24h > 0 },
    { label: '回复耗时 P50 / P95', value: `${formatDuration(runs.p50_seconds_24h)} / ${formatDuration(runs.p95_seconds_24h)}`,
      warn: false },
    { label: '待处理文档', value: String(documents.pending + documents.processing),
      warn: documents.oldest_pending_seconds > 300,
      hint: documents.pending ? `最久的已等 ${formatDuration(documents.oldest_pending_seconds)}` : undefined },
    { label: '处理失败的文档', value: String(documents.failed), warn: documents.failed > 0 },
    { label: '删除后的清理任务', value: String(purge.pending), warn: purge.retrying > 0,
      hint: purge.retrying ? `${purge.retrying} 个在重试（最多 ${purge.max_attempts} 次）` : undefined },
    { label: '执行中的 worker', value: String(s.busy_workers), warn: false },
  ]
}
