import MarkdownIt from 'markdown-it'

// html: false —— 模型回复里的原始 HTML 一律当文本显示，不会被浏览器执行（防 XSS）。
// markdown-it 默认还会拒绝 javascript: 之类的危险链接。
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

export function renderMarkdown(text: string): string {
  return md.render(text)
}
