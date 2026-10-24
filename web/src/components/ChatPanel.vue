<script setup lang="ts">
/**
 * 对话（decisions/0024 改版）：居中的阅读列；助手消息带标识、引用做成编号卡片；
 * Agent 正在做什么（工具调用、调研子任务）画成时间线；输入区固定在底部。
 * 状态（流式、取消、审批、接管）全部来自 pendingReply 状态机，这里只负责展示。
 */
import {
  ArrowUpOutlined,
  BookOutlined,
  CheckCircleFilled,
  CloseCircleFilled,
  DownloadOutlined,
  ExperimentOutlined,
  FileMarkdownOutlined,
  LoadingOutlined,
  MenuOutlined,
  RobotOutlined,
} from '@ant-design/icons-vue'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import type { AgentKind, Attachment, KnowledgeBase, Message } from '../api/types'
import { citedSources, formatSize } from '../composables/knowledge'
import { isActive, type PendingReply } from '../composables/pendingReply'
import type { RunNotice } from '../composables/runNotices'
import { describeError } from '../utils/errors'
import { renderMarkdown } from '../utils/markdown'

const props = defineProps<{
  title: string | null
  agent: AgentKind
  messages: Message[]
  notices: Map<string, RunNotice>
  pending: PendingReply | null
  error: string | null
  busy: boolean
  hasMore: boolean
  archived: boolean
  knowledgeBases: KnowledgeBase[]
  kbIds: string[]
  userId: string
  showMenu: boolean
}>()
const emit = defineEmits<{
  send: [text: string]
  stop: []
  retry: [runId: string]
  loadOlder: []
  unarchive: []
  setKbs: [ids: string[]]
  approve: [approved: boolean, feedback?: string]
  download: [attachment: Attachment]
  menu: []
}>()

const SLOW_HINT_SECONDS = 20 // 真实模型首字可能要几十秒；超过这个时间提示用户可以停止

const SUGGESTIONS: Record<AgentKind, string[]> = {
  chat: [
    '总结一下我关联的知识库里最重要的几条规定',
    '帮我分析这组数据并画个折线图：月份,销售额\n1,120\n2,180\n3,150\n4,210',
    'https://example.com 这个网页是做什么的？',
  ],
  research: [
    '对比 PostgreSQL 和 MongoDB 在多租户 SaaS 里的取舍',
    '调研 RAG 系统常见的评测方法，并给出落地建议',
    '整理知识库里关于退货和退款的所有规则，写成一份说明',
  ],
}

function noticeText(n: RunNotice) {
  return n.kind === 'cancelled' ? '已停止生成' : `回复失败：${describeError(n.errorCode)}`
}
// 当前这条回复对应的标注放在半截内容下面，其余的放在各自的用户消息下面
const pendingNotice = computed(() =>
  props.pending ? [...props.notices.values()].find((n) => n.runId === props.pending!.runId) ?? null : null,
)

const input = ref('')
const scroller = ref<HTMLElement | null>(null)
const feedback = ref('')

// “等待首字”计时：真实模型首字可能要几十秒，要让用户知道页面没卡死
const now = ref(Date.now())
const timer = setInterval(() => (now.value = Date.now()), 1000)
onBeforeUnmount(() => clearInterval(timer))
// 起点是服务端的 run 创建时间；浏览器与服务端的时钟可能有偏差，所以不显示负数
const waitedSeconds = computed(() =>
  props.pending ? Math.max(0, Math.floor((now.value - props.pending.startedAt) / 1000)) : 0,
)

const kbOptions = computed(() =>
  props.knowledgeBases.map((k) => ({ value: k.id, label: k.user_id === props.userId ? k.name : `${k.name}（部门共享）` })),
)
const empty = computed(() => !props.messages.length && !props.pending)

function submit() {
  const text = input.value.trim()
  if (!text || props.busy || props.archived) return
  emit('send', text)
  input.value = ''
}

function onKeydown(e: KeyboardEvent) {
  // Enter 发送，Shift+Enter 换行；输入法组字时（isComposing）按 Enter 是确认候选词，不能发送
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    submit()
  }
}

function revise() {
  const text = feedback.value.trim()
  if (!text) return
  emit('approve', false, text)
  feedback.value = ''
}

// 滚动策略：
// - 最后一条消息变了、或正在生成的回复有新内容 → 滚到底部；
// - 只是最前面多了消息（“加载更早的消息”）→ 保持用户正在看的位置，不能把人弹回底部。
// 注意要用“多个源”的写法：如果写成 watch(() => [a, b])，getter 每次返回新数组，
// Vue 按引用比较会认为每次都变了（第一版就是这样，导致加载更早后被拉回底部）。
watch(
  [
    () => props.messages[0]?.id,
    () => props.messages[props.messages.length - 1]?.id,
    () => props.pending?.text,
    () => props.pending?.steps.length,
  ],
  async ([first, last, text, steps], [prevFirst, prevLast, prevText, prevSteps]) => {
    const el = scroller.value
    if (!el) return
    const heightBefore = el.scrollHeight // 侦听器在 DOM 更新之前运行，这里是插入前的高度
    await nextTick()
    if (last !== prevLast || text !== prevText || steps !== prevSteps) el.scrollTo({ top: el.scrollHeight })
    else if (first !== prevFirst) el.scrollTop += el.scrollHeight - heightBefore
  },
)
</script>

<template>
  <div class="chat">
    <header class="bar">
      <a-button v-if="showMenu" type="text" aria-label="对话列表" @click="emit('menu')"><MenuOutlined /></a-button>
      <div class="bar-title">
        <span class="title-text">{{ title || '新对话' }}</span>
        <a-tag v-if="agent === 'research'" color="purple" class="mode"><ExperimentOutlined /> 深度调研</a-tag>
      </div>
      <!-- 对话关联的知识库（decisions/0018）：关联后 Agent 才会检索，回答附引用；部门共享的也可以关联（0023） -->
      <a-select
        mode="multiple"
        class="kb-select"
        :value="kbIds"
        :options="kbOptions"
        :max-tag-count="1"
        placeholder="关联知识库（可选）"
        :disabled="archived"
        @change="(v: unknown) => emit('setKbs', v as string[])"
      >
        <template #suffixIcon><BookOutlined /></template>
      </a-select>
    </header>

    <div ref="scroller" class="scroll">
      <div class="column">
        <div v-if="hasMore" class="older">
          <a-button size="small" @click="emit('loadOlder')">加载更早的消息</a-button>
        </div>

        <!-- 空对话：说明这个模式能做什么，给几个示例问题 -->
        <div v-if="empty" class="intro">
          <div class="intro-icon"><ExperimentOutlined v-if="agent === 'research'" /><RobotOutlined v-else /></div>
          <h2>{{ agent === 'research' ? '描述一个调研任务' : '有什么可以帮你？' }}</h2>
          <p class="an-muted">
            <template v-if="agent === 'research'">我会先把任务拆成几个子任务请你确认，再并行检索知识库和网页，最后汇总成一份可下载的报告。</template>
            <template v-else>可以直接提问；关联知识库后回答会附上引用。需要时我会加载技能：执行 Python 分析数据、抓取网页。</template>
          </p>
          <div class="suggestions">
            <button v-for="s in SUGGESTIONS[agent]" :key="s" class="suggestion" @click="input = s">{{ s.split('\n')[0] }}</button>
          </div>
        </div>

        <template v-for="m in messages" :key="m.id">
          <div v-if="m.role === 'user'" class="row user">
            <div class="bubble">{{ m.content }}</div>
          </div>
          <div v-else class="row assistant">
            <div class="avatar"><RobotOutlined /></div>
            <div class="body">
              <div class="an-md" v-html="renderMarkdown(m.content)" />
              <div v-if="m.attachments?.length" class="files">
                <div v-for="a in m.attachments" :key="a.id" class="file">
                  <FileMarkdownOutlined class="file-icon" />
                  <div class="file-meta">
                    <div class="file-name">{{ a.filename }}</div>
                    <div class="an-hint">{{ formatSize(a.size) }}</div>
                  </div>
                  <a-button size="small" @click="emit('download', a)"><DownloadOutlined /> 下载</a-button>
                </div>
              </div>
              <div v-if="citedSources(m.citations).length" class="sources">
                <div class="sources-label">引用来源</div>
                <div class="source-grid">
                  <a-popover v-for="c in citedSources(m.citations)" :key="c.n" placement="topLeft">
                    <template #title>{{ c.filename }}<template v-if="c.page"> · 第 {{ c.page }} 页</template></template>
                    <template #content><div class="source-full">{{ c.text }}</div></template>
                    <div class="source" tabindex="0">
                      <span class="source-n">{{ c.n }}</span>
                      <div class="source-meta">
                        <div class="source-name">{{ c.filename }}<template v-if="c.page"> · 第 {{ c.page }} 页</template></div>
                        <div class="source-text">{{ c.text }}</div>
                      </div>
                    </div>
                  </a-popover>
                </div>
              </div>
            </div>
          </div>
          <div
            v-if="notices.get(m.id) && notices.get(m.id)!.runId !== pending?.runId"
            :class="['notice', notices.get(m.id)!.kind]"
          >
            {{ noticeText(notices.get(m.id)!) }}
            <a-button v-if="notices.get(m.id)!.retryable" size="small" :disabled="busy" @click="emit('retry', notices.get(m.id)!.runId)">
              重试
            </a-button>
          </div>
        </template>

        <div v-if="pending" class="row assistant">
          <div class="avatar"><RobotOutlined /></div>
          <div class="body">
            <!-- 调研计划审批（decisions/0022） -->
            <div v-if="pending.status === 'awaiting' && pending.approval" class="approval an-card">
              <div class="approval-head">
                <span>请确认调研计划</span>
                <span class="an-hint">{{ pending.approval.plan.length }} 个子任务 · 预计调用模型 {{ pending.approval.estimate.model_calls }} 次</span>
              </div>
              <ol class="plan">
                <li v-for="(s, i) in pending.approval.plan" :key="i">
                  <div class="plan-title">{{ s.title }}</div>
                  <div class="an-muted">{{ s.question }}</div>
                </li>
              </ol>
              <a-textarea v-model:value="feedback" :auto-size="{ minRows: 1, maxRows: 4 }" placeholder="想调整计划？写下修改意见" />
              <div class="approval-actions">
                <a-button type="primary" @click="emit('approve', true)">按此计划执行</a-button>
                <a-button :disabled="!feedback.trim()" @click="revise">按意见修改</a-button>
                <a-button type="text" danger @click="emit('stop')">取消</a-button>
              </div>
            </div>

            <template v-else>
              <!-- Agent 正在做什么：调研子任务 + 工具调用时间线 -->
              <div v-if="pending.subtasks.length" class="subtasks">
                <div v-for="(s, i) in pending.subtasks" :key="i" :class="['subtask', s?.status]">
                  <LoadingOutlined v-if="s?.status === 'started'" />
                  <CheckCircleFilled v-else-if="s?.status === 'finished'" />
                  <CloseCircleFilled v-else />
                  <span>子任务 {{ i + 1 }} · {{ s?.title }}</span>
                </div>
              </div>
              <ul v-if="pending.steps.length" class="steps">
                <li v-for="(s, i) in pending.steps" :key="i" :class="['step', s.status]">
                  <LoadingOutlined v-if="s.status === 'running'" class="step-icon" />
                  <CheckCircleFilled v-else-if="s.status === 'done'" class="step-icon" />
                  <CloseCircleFilled v-else class="step-icon" />
                  <span class="step-label"><template v-if="s.subtask !== undefined">子任务 {{ s.subtask + 1 }} · </template>{{ s.label.replace(/^正在/, '') }}</span>
                  <code v-if="s.detail" class="step-detail">{{ s.detail }}</code>
                </li>
              </ul>

              <div v-if="pending.status === 'waiting'" class="waiting">
                <span class="dots"><i /><i /><i /></span>
                <span>{{ pending.activity ?? '思考中' }}</span>
                <span class="an-hint">{{ waitedSeconds }} 秒</span>
                <div v-if="waitedSeconds >= SLOW_HINT_SECONDS && !pending.subtasks.length" class="an-hint slow">
                  模型响应较慢，可以继续等待，或点击“停止”。
                </div>
              </div>
              <div v-if="pending.text" :class="['an-md', { interrupted: pending.status === 'failed' || pending.status === 'cancelled' }]">
                <div v-html="renderMarkdown(pending.text)" />
                <span v-if="pending.status === 'streaming'" class="cursor" />
              </div>
            </template>
          </div>
        </div>
        <div v-if="pendingNotice" :class="['notice', pendingNotice.kind]">
          {{ noticeText(pendingNotice) }}<template v-if="pending?.text">（已显示的部分没有保存）</template>
          <a-button v-if="pendingNotice.retryable" size="small" :disabled="busy" @click="emit('retry', pendingNotice.runId)">重试</a-button>
        </div>
      </div>
    </div>

    <div class="dock">
      <div class="column">
        <a-alert v-if="error" type="warning" show-icon class="alert" :message="error" />
        <a-alert v-if="archived" type="info" show-icon class="alert" message="这个对话已归档，恢复后才能继续对话。">
          <template #action><a-button size="small" @click="emit('unarchive')">恢复</a-button></template>
        </a-alert>
        <div :class="['composer', { disabled: archived }]">
          <a-textarea
            v-model:value="input"
            :bordered="false"
            :disabled="archived"
            :auto-size="{ minRows: 1, maxRows: 8 }"
            :placeholder="agent === 'research' ? '描述调研任务，例如：对比 A、B、C 三种方案的优缺点' : '输入消息'"
            @keydown="onKeydown"
          />
          <div class="composer-foot">
            <span class="an-hint">
              Enter 发送 · Shift+Enter 换行<template v-if="kbIds.length"> · 已关联 {{ kbIds.length }} 个知识库</template>
            </span>
            <!-- 生成中：“发送”变成“停止”。停止的结果以随后到达的 run.cancelled 事件为准 -->
            <a-button v-if="isActive(pending)" danger shape="round" size="small" @click="emit('stop')">停止</a-button>
            <a-button v-else type="primary" shape="circle" aria-label="发送" :loading="busy" :disabled="!input.trim() || archived" @click="submit">
              <ArrowUpOutlined />
            </a-button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chat {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.bar {
  display: flex;
  align-items: center;
  gap: 12px;
  height: 56px;
  flex: none;
  padding: 0 20px;
  border-bottom: 1px solid var(--an-border);
  background: var(--an-surface);
}
.bar-title {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
}
.title-text {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.mode {
  margin: 0;
}
.kb-select {
  width: 280px;
  flex: none;
}
.scroll {
  flex: 1;
  overflow-y: auto;
}
/* 阅读列：宽屏下一行不超过约 40 个汉字，长回答更好读 */
.column {
  max-width: 780px;
  margin: 0 auto;
  padding: 0 24px;
}
.scroll .column {
  padding-top: 28px;
  padding-bottom: 24px;
}
.older {
  text-align: center;
  margin-bottom: 16px;
}
.intro {
  text-align: center;
  padding: 56px 0 24px;
}
.intro-icon {
  width: 52px;
  height: 52px;
  margin: 0 auto 16px;
  display: grid;
  place-items: center;
  border-radius: 14px;
  font-size: 24px;
  background: var(--an-primary-soft);
  color: var(--an-primary);
}
.intro h2 {
  margin: 0 0 8px;
  font-size: 22px;
  font-weight: 600;
}
.intro p {
  max-width: 520px;
  margin: 0 auto;
}
.suggestions {
  display: grid;
  gap: 8px;
  max-width: 520px;
  margin: 24px auto 0;
}
.suggestion {
  padding: 10px 14px;
  border: 1px solid var(--an-border);
  border-radius: 10px;
  background: var(--an-surface);
  color: var(--an-text);
  text-align: left;
  cursor: pointer;
  transition: border-color 0.15s, background-color 0.15s;
}
.suggestion:hover {
  border-color: var(--an-primary);
  background: var(--an-primary-soft);
}
.row {
  display: flex;
  margin-bottom: 22px;
}
.row.user {
  justify-content: flex-end;
}
.row.user .bubble {
  max-width: 80%;
  padding: 10px 14px;
  border-radius: 14px 14px 4px 14px;
  background: var(--an-primary-soft);
  color: var(--an-text);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.row.assistant {
  gap: 12px;
}
.avatar {
  width: 30px;
  height: 30px;
  flex: none;
  display: grid;
  place-items: center;
  border-radius: 8px;
  background: var(--an-surface);
  border: 1px solid var(--an-border);
  color: var(--an-primary);
}
.body {
  flex: 1;
  min-width: 0;
  padding-top: 3px;
}
.files {
  display: grid;
  /* minmax(0, 1fr)：网格项默认 min-width: auto，不换行的长文件名会把整列撑宽（手机宽度下出现横向滚动，实测踩到） */
  grid-template-columns: minmax(0, 1fr);
  gap: 8px;
  margin-top: 12px;
}
.file {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid var(--an-border);
  border-radius: 10px;
  background: var(--an-surface);
}
.file-icon {
  font-size: 22px;
  color: var(--an-primary);
}
.file-meta {
  flex: 1;
  min-width: 0;
}
.file-name {
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sources {
  margin-top: 14px;
}
.sources-label {
  font-size: 12px;
  color: var(--an-text-3);
  margin-bottom: 6px;
}
.source-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 8px;
}
.source {
  display: flex;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--an-border);
  border-radius: 8px;
  background: var(--an-surface);
  cursor: default;
}
.source:hover,
.source:focus-visible {
  border-color: var(--an-primary);
  outline: none;
}
.source-n {
  flex: none;
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
  border-radius: 5px;
  font-size: 12px;
  font-weight: 600;
  background: var(--an-primary-soft);
  color: var(--an-primary-text);
}
.source-meta {
  min-width: 0;
}
.source-name {
  font-size: 12px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.source-text {
  font-size: 12px;
  color: var(--an-text-2);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.source-full {
  max-width: 480px;
  max-height: 320px;
  overflow-y: auto;
  white-space: pre-wrap;
  font-size: 12px;
  line-height: 1.7;
}
.notice {
  display: flex;
  gap: 8px;
  align-items: center;
  justify-content: flex-end;
  margin: -12px 0 20px;
  font-size: 12px;
}
.notice.failed {
  color: var(--an-danger);
}
.notice.cancelled {
  color: var(--an-text-3);
}
.approval {
  padding: 16px 18px;
  border-color: var(--an-primary);
}
.approval-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  font-weight: 600;
  margin-bottom: 10px;
}
.plan {
  margin: 0 0 12px;
  padding-left: 20px;
}
.plan li + li {
  margin-top: 8px;
}
.plan-title {
  font-weight: 500;
}
.approval-actions {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}
.subtasks {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
}
.subtask {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  border-radius: 999px;
  border: 1px solid var(--an-border);
  background: var(--an-surface);
  font-size: 12px;
  color: var(--an-text-2);
}
.subtask.finished {
  color: var(--an-success);
  border-color: var(--an-success-soft);
  background: var(--an-success-soft);
}
.subtask.failed {
  color: var(--an-danger);
  background: var(--an-danger-soft);
}
.steps {
  list-style: none;
  margin: 0 0 10px;
  padding: 0 0 0 2px;
  border-left: 2px solid var(--an-border);
}
.step {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 0 3px 12px;
  font-size: 13px;
  color: var(--an-text-2);
  min-width: 0;
}
.step-icon {
  margin-left: -19px;
  background: var(--an-bg);
  border-radius: 50%;
}
.step.done .step-icon {
  color: var(--an-success);
}
.step.failed .step-icon {
  color: var(--an-danger);
}
.step.running .step-icon {
  color: var(--an-primary);
}
.step-label {
  flex: none;
}
.step-detail {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--an-mono);
  font-size: 12px;
  padding: 0 6px;
  border-radius: 4px;
  background: var(--an-surface-2);
  color: var(--an-text-2);
}
.waiting {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  color: var(--an-text-2);
  font-size: 13px;
}
.slow {
  width: 100%;
}
.dots {
  display: inline-flex;
  gap: 3px;
}
.dots i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--an-primary);
  animation: pulse 1.2s infinite ease-in-out;
}
.dots i:nth-child(2) {
  animation-delay: 0.15s;
}
.dots i:nth-child(3) {
  animation-delay: 0.3s;
}
@keyframes pulse {
  0%,
  80%,
  100% {
    opacity: 0.25;
  }
  40% {
    opacity: 1;
  }
}
.interrupted {
  padding: 8px 12px;
  border: 1px dashed var(--an-danger);
  border-radius: 8px;
  background: var(--an-danger-soft);
}
.cursor {
  display: inline-block;
  width: 7px;
  height: 1.1em;
  margin-left: 2px;
  vertical-align: text-bottom;
  background: var(--an-primary);
  animation: blink 1s steps(1) infinite;
}
@keyframes blink {
  50% {
    opacity: 0;
  }
}
.dock {
  flex: none;
  padding: 8px 0 18px;
  background: linear-gradient(to bottom, transparent, var(--an-bg) 24px);
}
.alert {
  margin-bottom: 10px;
}
.composer {
  border: 1px solid var(--an-border-strong);
  border-radius: 14px;
  background: var(--an-surface);
  box-shadow: var(--an-shadow);
  padding: 8px 10px 8px 6px;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.composer:focus-within {
  border-color: var(--an-primary);
  box-shadow: 0 0 0 3px var(--an-primary-soft);
}
.composer.disabled {
  opacity: 0.6;
}
.composer-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-left: 8px;
}
@media (max-width: 720px) {
  .bar {
    padding: 0 10px;
    gap: 6px;
  }
  .kb-select {
    width: 130px;
  }
  .mode {
    display: none;
  }
  .source-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .column {
    padding: 0 14px;
  }
  .row.user .bubble {
    max-width: 92%;
  }
}
@media (prefers-reduced-motion: reduce) {
  .dots i,
  .cursor {
    animation: none;
  }
}
</style>
