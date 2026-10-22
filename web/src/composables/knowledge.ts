import type { DocumentStatus, KnowledgeDocument } from '../api/types'

/** 还有文档在排队或处理中：需要继续轮询状态 */
export function needsPolling(docs: KnowledgeDocument[]): boolean {
  return docs.some((d) => d.status === 'pending' || d.status === 'processing')
}

export const STATUS_TAG: Record<DocumentStatus, { color: string; label: string }> = {
  pending: { color: 'default', label: '排队中' },
  processing: { color: 'processing', label: '处理中' },
  ready: { color: 'success', label: '可检索' },
  failed: { color: 'error', label: '失败' },
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

const TIMING_LABELS: Record<string, string> = { embed: '向量化', vector: '向量检索', fetch: '取原文', rerank: '重排' }

/** 检索各步耗时，按固定顺序展示：向量化 12ms · 向量检索 3ms · … */
export function formatTimings(timings: Record<string, number>): string {
  return Object.keys(TIMING_LABELS)
    .filter((k) => k in timings)
    .map((k) => `${TIMING_LABELS[k]} ${Math.round(timings[k])}ms`)
    .join(' · ')
}
