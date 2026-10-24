import { describe, expect, it } from 'vitest'
import type { Attachment } from '../api/types'
import { fileKind, previewUrl } from '../composables/attachments'

const att = (content_type: string): Attachment => ({ id: 'a1', filename: 'f', content_type, size: 1 })

describe('attachments', () => {
  it('previews common raster images only', () => {
    expect(previewUrl(att('image/png'))).toBe('/api/artifacts/a1/download')
    expect(previewUrl(att('image/svg+xml'))).toBeNull() // 可以带脚本的格式不预览
    expect(previewUrl(att('application/octet-stream'))).toBeNull()
  })

  it('picks an icon kind from the content type', () => {
    expect(fileKind(att('image/jpeg'))).toBe('image')
    expect(fileKind(att('text/csv; charset=utf-8'))).toBe('table')
    expect(fileKind(att('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'))).toBe('table')
    expect(fileKind(att('text/markdown; charset=utf-8'))).toBe('markdown')
    expect(fileKind(att('application/octet-stream'))).toBe('file')
  })
})
