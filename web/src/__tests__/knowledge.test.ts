import { describe, expect, it } from 'vitest'
import type { KnowledgeDocument } from '../api/types'
import { formatSize, formatTimings, needsPolling } from '../composables/knowledge'
import { describeDocumentError, describeRequestError } from '../utils/errors'

const doc = (status: KnowledgeDocument['status']): KnowledgeDocument => ({
  id: status, knowledge_base_id: 'kb', filename: 'a.txt', size_bytes: 1, status,
  error_code: null, chunk_count: 0, created_at: '', processed_at: null,
})

describe('knowledge helpers', () => {
  it('polls only while something is pending or processing', () => {
    expect(needsPolling([])).toBe(false)
    expect(needsPolling([doc('ready'), doc('failed')])).toBe(false)
    expect(needsPolling([doc('ready'), doc('pending')])).toBe(true)
    expect(needsPolling([doc('processing')])).toBe(true)
  })

  it('formats sizes and timings in a fixed order', () => {
    expect(formatSize(512)).toBe('512 B')
    expect(formatSize(3.2 * 1024 * 1024)).toBe('3.2 MB')
    expect(formatTimings({ rerank: 980.4, embed: 3.2, fetch: 2, vector: 4.6 })).toBe('向量化 3ms · 向量检索 5ms · 取原文 2ms · 重排 980ms')
    expect(formatTimings({ embed: 1, vector: 2, fetch: 3 })).not.toContain('重排')
  })

  it('maps error codes to readable copy, with fallbacks', () => {
    expect(describeDocumentError('encrypted')).toContain('加密')
    expect(describeDocumentError('something_new')).toBe(describeDocumentError('internal'))
    expect(describeRequestError('document_exists')).toContain('相同')
    expect(describeRequestError('unknown_code')).toBeNull()
    expect(describeRequestError(null)).toBeNull()
  })
})

describe('citations', () => {
  const c = (n: number, cited: boolean, page: number | null = null) => ({
    n, cited, page, chunk_id: `c${n}`, document_id: 'd', knowledge_base_id: 'k', filename: '退货.txt', seq: 0,
    text: '...', vector_score: 0.5, rerank_score: 1,
  })

  it('lists only cited sources in order, with page when known', async () => {
    const { citedSources, sourceLabel } = await import('../composables/knowledge')
    expect(citedSources([c(3, true), c(1, false), c(2, true, 5)]).map(sourceLabel)).toEqual(['[2] 退货.txt · 第 5 页', '[3] 退货.txt'])
    expect(citedSources(null)).toEqual([])
  })
})
