import { describe, expect, it } from 'vitest'
import { renderMarkdown } from '../utils/markdown'

describe('renderMarkdown', () => {
  it('renders markdown', () => {
    expect(renderMarkdown('**粗体**')).toContain('<strong>粗体</strong>')
  })

  it('never emits raw HTML from model output (XSS)', () => {
    const html = renderMarkdown('<script>alert(1)</script><img src=x onerror=alert(1)>')
    expect(html).not.toContain('<script')
    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;script&gt;')
  })

  it('does not turn javascript: URLs into links', () => {
    expect(renderMarkdown('[点我](javascript:alert(1))')).not.toContain('href="javascript:')
  })
})
