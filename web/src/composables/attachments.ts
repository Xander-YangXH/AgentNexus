import type { Attachment } from '../api/types'

// 回复的附件（调研报告、share_file 交付的图表和表格，阶段 7.2）：按类型选图标，图片直接预览。
// 预览只用 <img>：浏览器在 <img> 里不会执行脚本；服务端也只给安全的格式保留类型（SVG、HTML 一律按二进制下载）
const PREVIEWABLE = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp'])

function baseType(contentType: string): string {
  return contentType.split(';')[0].trim().toLowerCase()
}

export function previewUrl(a: Attachment): string | null {
  return PREVIEWABLE.has(baseType(a.content_type)) ? `/api/artifacts/${a.id}/download` : null
}

export type FileKind = 'image' | 'table' | 'markdown' | 'file'

export function fileKind(a: Attachment): FileKind {
  const t = baseType(a.content_type)
  if (t.startsWith('image/')) return 'image'
  if (t === 'text/csv' || t.includes('spreadsheet')) return 'table'
  if (t === 'text/markdown') return 'markdown'
  return 'file'
}
