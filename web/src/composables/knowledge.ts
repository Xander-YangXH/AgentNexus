import type { Citation, DocumentStatus, KnowledgeDocument } from '../api/types'

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

/** 回答下方展示的来源：只列回答里实际标注了的 [n]，按编号排序；同一文件的多个片段各占一行（页码 / 块号不同） */
export function citedSources(citations: Citation[] | null | undefined): Citation[] {
  return (citations ?? []).filter((c) => c.cited).sort((a, b) => a.n - b.n)
}

/**
 * 引用片段的展示文本：去掉 Markdown 标题符号、合并空行。
 * 分块保留了原文（含“# 售后政策 ## 退货政策”这样的标题行，检索时有用），直接显示在引用卡片里很乱（截图时发现）
 */
export function plainSnippet(text: string): string {
  return text
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/\n{2,}/g, '\n')
    .trim()
}

export function sourceLabel(c: Citation): string {
  return `[${c.n}] ${c.filename}${c.page ? ` · 第 ${c.page} 页` : ''}`
}
