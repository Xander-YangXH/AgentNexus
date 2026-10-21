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
