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
