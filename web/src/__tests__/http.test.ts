import { describe, expect, it } from 'vitest'
import { HttpError, parseErrorBody } from '../api/http'

describe('error body', () => {
  it('parses the unified error format', () => {
    const body = parseErrorBody('{"error":{"code":"run_conflict","message":"busy","request_id":"abc"}}')
    expect(body?.code).toBe('run_conflict')
  })

  it('returns null for non-JSON or unexpected bodies (e.g. a proxy HTML error page)', () => {
    expect(parseErrorBody('<html>502 Bad Gateway</html>')).toBeNull()
    expect(parseErrorBody('{"detail":"old format"}')).toBeNull()
  })

  it('shows the request id for server errors so users can report it', () => {
    const e = new HttpError(500, { code: 'internal_error', message: 'internal server error', request_id: 'abc123' })
    expect(e.describe()).toContain('abc123')
    expect(new HttpError(409, { code: 'run_conflict', message: 'busy', request_id: 'x' }).describe()).toBe('busy')
    expect(new HttpError(502, null).describe()).toBe('HTTP 502')
  })
})
