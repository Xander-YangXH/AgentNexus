// 后端只给错误码（runtime/failures.py），面向用户的文案在这里。新增错误码时两边一起改
const COPY: Record<string, string> = {
  timeout: '模型响应超时',
  llm_rate_limited: '请求太频繁或模型额度已用完，请稍后再试',
  llm_unavailable: '模型服务暂时不可用（可能是负载过高），请稍后重试',
  llm_config: '模型配置有误，请检查密钥和模型名',
  interrupted: '服务重启，生成被中断',
  internal: '生成失败（服务内部错误）',
}

export function describeError(code: string | null | undefined): string {
  return COPY[code ?? 'internal'] ?? COPY.internal // 不认识的错误码也给出可读的提示
}

// 知识库：文档处理失败的原因（后端 knowledge/parsing.py 与 services/knowledge.py）
const DOCUMENT_COPY: Record<string, string> = {
  unsupported_type: '不支持的文件类型',
  encrypted: 'PDF 已加密，无法读取',
  empty: '没有可提取的文字（扫描件需要 OCR，暂不支持）',
  corrupt: '文件已损坏或格式不对',
  index_unavailable: '向量库暂时不可用，可以稍后重新处理',
  internal: '处理失败（服务内部错误）',
}

export function describeDocumentError(code: string | null | undefined): string {
  return DOCUMENT_COPY[code ?? 'internal'] ?? DOCUMENT_COPY.internal
}

// 知识库接口的 4xx 错误码 → 用户能看懂的提示（后端 message 是英文，给开发者看的）
const REQUEST_COPY: Record<string, string> = {
  document_exists: '这个知识库里已经有内容完全相同的文件',
  file_too_large: '文件太大（默认上限 10MB）',
  unsupported_file_type: '只支持 .txt .md .pdf .docx',
  empty_file: '文件是空的',
  knowledge_base_exists: '已有同名知识库',
  document_not_retryable: '只有处理失败的文档才能重新处理',
}

export function describeRequestError(code: string | null | undefined): string | null {
  return code ? (REQUEST_COPY[code] ?? null) : null
}
