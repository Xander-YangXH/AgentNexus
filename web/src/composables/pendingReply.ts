/**
 * “正在生成的那条回复”的状态机（纯函数，便于单测）。
 *
 *   waiting ──token──▶ streaming ──run.completed──▶ completed
 *      │                   │
 *      ├──run.failed───────┼──▶ failed（保留已收到的半截文本）
 *      └──run.cancelled────┴──▶ cancelled（用户点了“停止”，同样保留半截文本）
 *
 * 为什么 completed 之后不直接用拼好的文本当最终结果：数据库才是事实来源（decisions/0006/0007），
 * 完成后重新拉一次消息列表，用库里的助手消息替换掉这条临时回复。
 * 失败和取消的半截文本都不入库（decisions/0007/0009），只在当前页面上显示。
 *
 * 知识库检索（decisions/0018）：tool.started 时清空已显示的文字——模型在调用工具前说的话不会成为最终回复
 * （最终回复取自图的最终状态），并显示“正在检索”；tool.finished 后回到“等待输出”，直到回答的第一个字。
 *
 * 通用工具（decisions/0021）：按工具名显示“正在执行代码”“正在抓取网页”等；不认识的工具显示“正在调用 xxx”。
 *
 * 审批（decisions/0022）：run.awaiting_approval → awaiting（显示计划和“批准 / 修改 / 取消”），服务端在这里结束事件流；
 * 记下这个事件的 id，答复后从它之后重新订阅。批准后的执行会重新发 run.started，回到 waiting。
 * 审批后的执行 attempt 从 1 重新计（服务端清零了），所以进入 awaiting 时把 attempt 也重置为 1，否则新事件会被当成“旧尝试的残余”丢掉。
 * 调研的子任务进度（research.subtask）显示成“子任务 2/3 完成：…”。
 *
 * 接管（decisions/0020）：执行它的 worker 中断后，别的 worker 从 checkpoint 接着做，先发 run.resumed {attempt}。
 * 这时清空已显示的半截文字（新的执行会重新流出被打断的那一步）；之后只接受这一次尝试的事件——
 * 旧 worker 在心跳发现自己被接管之前，可能还会流出几个 token，它们带着更小的 attempt，直接忽略。
 */
import type { ResearchPlanRequest, RunEvent } from '../api/types'

export type PendingStatus = 'waiting' | 'streaming' | 'awaiting' | 'completed' | 'failed' | 'cancelled'

/** 一次工具调用（decisions/0024：界面上画成时间线，让“Agent 正在做什么”看得见） */
export interface ToolStep {
  name: string
  label: string // 进行中的说法，如“正在执行代码”
  detail: string // 参数摘要：检索词、网址、代码第一行……
  status: 'running' | 'done' | 'failed'
  subtask?: number // 调研：属于第几个子任务
}

export interface SubtaskProgress {
  title: string
  status: 'started' | 'finished' | 'failed'
}

export interface PendingReply {
  runId: string
  status: PendingStatus
  text: string
  errorCode: string | null
  startedAt: number // 用于显示“等待首字 N 秒”，取服务端的 run 创建时间
  activity: string | null // 正在做什么（如“正在检索：退货政策”），等待阶段显示
  attempt: number // 当前接受的是第几次执行的事件（被接管后递增）
  approval: ResearchPlanRequest | null // 等待审批的内容
  resumeFrom: string | null // 审批事件的 id：答复后从它之后重新订阅
  subtasks: SubtaskProgress[] // 调研子任务进度，按 index
  steps: ToolStep[] // 本次回答的工具调用，按发生顺序
}

export function newPending(runId: string, now: number = Date.now()): PendingReply {
  return {
    runId, status: 'waiting', text: '', errorCode: null, startedAt: now, activity: null, attempt: 1,
    approval: null, resumeFrom: null, subtasks: [], steps: [],
  }
}

export function isActive(p: PendingReply | null): boolean {
  return p?.status === 'waiting' || p?.status === 'streaming' || p?.status === 'awaiting'
}

const TOOL_LABELS: Record<string, [string, string]> = {
  // 工具名 -> [进行中, 完成后]
  knowledge_search: ['正在检索知识库', ''],
  load_skill: ['正在加载技能', '技能已加载'],
  python_exec: ['正在执行代码', '代码执行完成'],
  list_files: ['正在查看文件', '文件列表已获取'],
  read_file: ['正在读取文件', '文件已读取'],
  write_file: ['正在写入文件', '文件已写入'],
  share_file: ['正在准备下载文件', '文件已可下载'],
  fetch: ['正在抓取网页', '网页已获取'],
}

export function toolLabel(name: string): string {
  return TOOL_LABELS[name]?.[0] ?? `正在调用 ${name}`
}

export function toolActivity(name: string, detail: string): string {
  const label = toolLabel(name)
  return detail ? `${label}：${detail}` : label
}

/** tool.finished 不带调用 id：按“同名、同子任务、还在进行中”的最后一个匹配（并行的同名调用顺序无关紧要，都会被结束） */
function finishStep(steps: ToolStep[], name: string, subtask: number | undefined, ok: boolean): ToolStep[] {
  for (let i = steps.length - 1; i >= 0; i--) {
    const s = steps[i]
    if (s.status === 'running' && s.name === name && s.subtask === subtask) {
      const next = [...steps]
      next[i] = { ...s, status: ok ? 'done' : 'failed' }
      return next
    }
  }
  return steps
}

function toolDone(name: string, hits: number, ok: boolean): string {
  if (name === 'knowledge_search') {
    return ok ? `检索到 ${hits} 条资料，正在生成回答` : '知识库检索失败，正在生成回答'
  }
  const label = TOOL_LABELS[name]?.[1] || `${name} 已完成`
  return ok ? `${label}，继续处理` : `${name} 执行失败，正在处理`
}

export function applyRunEvent(p: PendingReply, e: RunEvent): PendingReply {
  if (!isActive(p)) return p // 终态之后的事件忽略
  // 第一次执行的事件不带 attempt（后端只在接管后才加），当作 1。第一版写成“有 attempt 才比较”，
  // 第一次执行的残余 token 就漏过去了（补测试时发现）
  const attempt = (e.data as { attempt?: number }).attempt ?? 1
  // 只过滤 Agent 执行中产生的事件（token、工具、子任务进度）。run.* 生命周期事件由派发器发布、不带 attempt，
  // 第一版对它们也做了过滤：接管后的 run.completed、run.awaiting_approval 都会被当成“旧尝试的残余”丢掉
  // （完成时恰好被“流结束后从数据库重新加载”掩盖了，审批卡片则会直接不显示；补审批测试时发现）
  if (!e.type.startsWith('run.') && attempt < p.attempt) return p // 被接管前那次执行的残余事件
  switch (e.type) {
    case 'run.resumed':
      if (e.data.attempt <= p.attempt) return p
      return { ...p, status: 'waiting', text: '', attempt: e.data.attempt, steps: [], activity: '执行中断，已由其他进程接着生成' }
    case 'run.started':
      // 审批后的继续执行：回到等待输出
      return p.status === 'awaiting' ? { ...p, status: 'waiting', approval: null, activity: '已确认计划，开始执行' } : p
    case 'run.awaiting_approval':
      return { ...p, status: 'awaiting', text: '', activity: null, approval: e.data.request, resumeFrom: e.id ?? p.resumeFrom, attempt: 1 }
    case 'research.subtask': {
      const subtasks = [...p.subtasks]
      subtasks[e.data.index] = { title: e.data.title, status: e.data.status }
      const done = subtasks.filter((s) => s && s.status !== 'started').length
      const verb = e.data.status === 'started' ? '开始' : e.data.status === 'finished' ? '完成' : '失败'
      return { ...p, status: 'waiting', subtasks, activity: `子任务 ${done}/${e.data.total}，${verb}：${e.data.title}` }
    }
    case 'token':
      return { ...p, status: 'streaming', text: p.text + e.data.text, activity: null }
    case 'tool.started': {
      // 旧版后端只有 query，没有 detail
      const label = toolActivity(e.data.name, e.data.detail ?? e.data.query)
      const prefix = e.data.subtask !== undefined ? `子任务 ${e.data.subtask + 1} ` : ''
      const step: ToolStep = {
        name: e.data.name, label: toolLabel(e.data.name), detail: e.data.detail ?? e.data.query ?? '',
        status: 'running', subtask: e.data.subtask,
      }
      return { ...p, status: 'waiting', text: '', activity: prefix + label, steps: [...p.steps, step] }
    }
    case 'tool.finished':
      return { ...p, activity: toolDone(e.data.name, e.data.hits, e.data.ok),
               steps: finishStep(p.steps, e.data.name, e.data.subtask, e.data.ok) }
    case 'run.completed':
      return { ...p, status: 'completed' }
    case 'run.failed':
      return { ...p, status: 'failed', errorCode: e.data.error_code ?? 'internal' }
    case 'run.cancelled':
      return { ...p, status: 'cancelled' }
    default:
      // 不认识的事件（后端新增了类型、前端还是旧版本）忽略。第一版没有 default，会返回 undefined 把状态弄坏
      return p
  }
}
