import { describe, expect, it } from 'vitest'
import { describeDocumentError, describeLoginError, describeRequestError } from '../utils/errors'

describe('login errors', () => {
  it('does not tell whether the user exists', () => {
    expect(describeLoginError(401)).toBe('用户名或密码错误')
  })

  it('shows how long to wait when throttled (rounded up to minutes)', () => {
    expect(describeLoginError(429, { retry_after: 61 })).toBe('尝试次数过多，请 2 分钟后再试')
    expect(describeLoginError(429, { retry_after: 5 })).toBe('尝试次数过多，请 1 分钟后再试')
    expect(describeLoginError(429)).toBe('尝试次数过多，请稍后再试')
  })

  it('leaves other errors to the generic description', () => {
    expect(describeLoginError(500)).toBeNull()
  })
})

describe('document errors', () => {
  it('explains isolated parsing failures', () => {
    expect(describeDocumentError('parse_timeout')).toContain('超时')
    expect(describeDocumentError('too_complex')).toContain('内存')
    expect(describeDocumentError('something_new')).toBe(describeDocumentError('internal'))
  })
})

describe('request errors', () => {
  it('explains why a conversation cannot be deleted while a reply is in progress', () => {
    expect(describeRequestError('run_conflict')).toContain('请先停止')
  })
})

