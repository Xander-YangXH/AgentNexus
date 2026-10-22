<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import type { AgentKind, Attachment, KnowledgeBase, Message } from '../api/types'
import { citedSources, sourceLabel } from '../composables/knowledge'
import { isActive, type PendingReply } from '../composables/pendingReply'
import type { RunNotice } from '../composables/runNotices'
import { describeError } from '../utils/errors'
import { renderMarkdown } from '../utils/markdown'

const props = defineProps<{
  messages: Message[]
  notices: Map<string, RunNotice>
  pending: PendingReply | null
  error: string | null
  busy: boolean
  hasMore: boolean
  archived: boolean
  knowledgeBases: KnowledgeBase[]
  kbIds: string[]
  agent: AgentKind
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
}>()

// 审批卡片（decisions/0022）：批准，或写修改意见让它重新制定计划；不想继续用“停止”（取消）
const feedback = ref('')
function revise() {
  const text = feedback.value.trim()
  if (!text) return
  emit('approve', false, text)
  feedback.value = ''
}
function sizeLabel(n: number) {
  return n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`
}

const SLOW_HINT_SECONDS = 20 // Gemini 免费档首字实测 7~173 秒；超过这个时间提示用户可以停止

function noticeText(n: RunNotice) {
  return n.kind === 'cancelled' ? '已停止生成' : `回复失败：${describeError(n.errorCode)}`
}
// 当前这条回复对应的标注放在半截内容下面，其余的放在各自的用户消息下面
const pendingNotice = computed(() =>
  props.pending ? [...props.notices.values()].find((n) => n.runId === props.pending!.runId) ?? null : null,
)

const input = ref('')
const scroller = ref<HTMLElement | null>(null)

// “等待首字”计时：真实模型首字可能要几十秒（Gemini 免费档实测 7~173 秒），要让用户知道页面没卡死
const now = ref(Date.now())
const timer = setInterval(() => (now.value = Date.now()), 1000)
onBeforeUnmount(() => clearInterval(timer))
// 起点是服务端的 run 创建时间；浏览器与服务端的时钟可能有偏差，所以不显示负数
const waitedSeconds = computed(() =>
  props.pending ? Math.max(0, Math.floor((now.value - props.pending.startedAt) / 1000)) : 0,
)

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

// 滚动策略：
// - 最后一条消息变了、或正在生成的回复有新内容 → 滚到底部；
// - 只是最前面多了消息（“加载更早的消息”）→ 保持用户正在看的位置，不能把人弹回底部。
// 注意要用“多个源”的写法：如果写成 watch(() => [a, b])，getter 每次返回新数组，
// Vue 按引用比较会认为每次都变了（第一版就是这样，导致加载更早后被拉回底部）。
watch(
  [() => props.messages[0]?.id, () => props.messages[props.messages.length - 1]?.id, () => props.pending?.text],
  async ([first, last, text], [prevFirst, prevLast, prevText]) => {
    const el = scroller.value
    if (!el) return
    const heightBefore = el.scrollHeight // 侦听器在 DOM 更新之前运行，这里是插入前的高度
    await nextTick()
    if (last !== prevLast || text !== prevText) el.scrollTo({ top: el.scrollHeight })
    else if (first !== prevFirst) el.scrollTop += el.scrollHeight - heightBefore
  },
)
</script>

<template>
  <div class="panel">
    <!-- 对话关联的知识库（decisions/0018）：关联后 Agent 才会检索，回答附引用 -->
    <div class="kb-bar">
      <span class="kb-label">知识库</span>
      <a-select
        mode="multiple"
        :value="kbIds"
        :options="knowledgeBases.map((k) => ({ value: k.id, label: k.name }))"
        placeholder="不使用知识库（直接对话）"
        style="flex: 1"
        :disabled="archived"
        @change="(v: unknown) => emit('setKbs', v as string[])"
      />
    </div>
    <div ref="scroller" class="messages">
      <div v-if="hasMore" class="older">
        <a-button size="small" @click="emit('loadOlder')">加载更早的消息</a-button>
      </div>
      <template v-for="m in messages" :key="m.id">
        <div :class="['row', m.role]">
          <div v-if="m.role === 'assistant'" class="bubble md">
            <div v-html="renderMarkdown(m.content)" />
            <div v-if="m.attachments?.length" class="attachments">
              <a-button v-for="a in m.attachments" :key="a.id" size="small" @click="emit('download', a)">
                下载 {{ a.filename }}（{{ sizeLabel(a.size) }}）
              </a-button>
            </div>
            <div v-if="citedSources(m.citations).length" class="sources">
              <a-popover v-for="c in citedSources(m.citations)" :key="c.n" :title="sourceLabel(c)" placement="topLeft">
                <template #content><div class="source-text">{{ c.text }}</div></template>
                <a-tag class="source">{{ sourceLabel(c) }}</a-tag>
              </a-popover>
            </div>
          </div>
          <div v-else class="bubble plain">{{ m.content }}</div>
        </div>
        <div
          v-if="notices.get(m.id) && notices.get(m.id)!.runId !== pending?.runId"
          :class="['notice', notices.get(m.id)!.kind]"
        >
          {{ noticeText(notices.get(m.id)!) }}
          <a-button
            v-if="notices.get(m.id)!.retryable"
            size="small"
            :disabled="busy"
            @click="emit('retry', notices.get(m.id)!.runId)"
          >
            重试
          </a-button>
        </div>
      </template>

      <div v-if="pending" class="row assistant">
        <div v-if="pending.status === 'awaiting' && pending.approval" class="bubble approval">
          <div class="approval-title">请确认调研计划（共 {{ pending.approval.plan.length }} 个子任务，预计调用模型 {{ pending.approval.estimate.model_calls }} 次）</div>
          <ol class="plan">
            <li v-for="(s, i) in pending.approval.plan" :key="i">
              <b>{{ s.title }}</b>：{{ s.question }}
            </li>
          </ol>
          <a-textarea v-model:value="feedback" :auto-size="{ minRows: 1, maxRows: 4 }" placeholder="想调整计划？写下修改意见" />
          <div class="approval-actions">
            <a-button type="primary" @click="emit('approve', true)">按此计划执行</a-button>
            <a-button :disabled="!feedback.trim()" @click="revise">按意见修改计划</a-button>
            <a-button danger @click="emit('stop')">取消</a-button>
          </div>
        </div>
        <div v-else-if="pending.status === 'waiting'" class="bubble waiting">
          <a-spin size="small" /> {{ pending.activity ?? '等待模型开始输出…' }} {{ waitedSeconds }} 秒
          <ul v-if="pending.subtasks.length" class="subtasks">
            <li v-for="(s, i) in pending.subtasks" :key="i" :class="s?.status">
              {{ s?.status === 'finished' ? '✓' : s?.status === 'failed' ? '✗' : '…' }} {{ s?.title }}
            </li>
          </ul>
          <div v-if="waitedSeconds >= SLOW_HINT_SECONDS && !pending.subtasks.length" class="slow">模型响应较慢，可以继续等待，或点击“停止”。</div>
        </div>
        <div
          v-else-if="pending.text"
          :class="['bubble', 'md', { interrupted: pending.status === 'failed' || pending.status === 'cancelled' }]"
        >
          <div v-html="renderMarkdown(pending.text)" />
          <span v-if="pending.status === 'streaming'" class="cursor">▍</span>
        </div>
      </div>
      <div v-if="pendingNotice" :class="['notice', pendingNotice.kind]">
        {{ noticeText(pendingNotice) }}<template v-if="pending?.text">（已显示的部分没有保存）</template>
        <a-button v-if="pendingNotice.retryable" size="small" :disabled="busy" @click="emit('retry', pendingNotice.runId)">
          重试
        </a-button>
      </div>
    </div>

    <a-alert v-if="error" type="warning" show-icon class="alert" :message="error" />
    <a-alert v-if="archived" type="info" show-icon class="alert" message="这个对话已归档，恢复后才能继续对话。">
      <template #action>
        <a-button size="small" @click="emit('unarchive')">恢复</a-button>
      </template>
    </a-alert>
    <div class="composer">
      <a-textarea
        v-model:value="input"
        :disabled="archived"
        :auto-size="{ minRows: 2, maxRows: 6 }"
        :placeholder="agent === 'research' ? '描述调研任务（例如：对比 A、B、C 三种方案的优缺点），会先给出计划请你确认' : '输入消息，Enter 发送，Shift+Enter 换行'"
        @keydown="onKeydown"
      />
      <!-- 生成中：“发送”变成“停止”。停止的结果以随后到达的 run.cancelled 事件为准 -->
      <a-button v-if="isActive(pending)" danger @click="emit('stop')">停止</a-button>
      <a-button v-else type="primary" :loading="busy" :disabled="!input.trim() || archived" @click="submit">发送</a-button>
    </div>
  </div>
</template>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.kb-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 24px;
  border-bottom: 1px solid #f0f0f0;
}
.kb-label {
  color: #888;
  font-size: 12px;
  white-space: nowrap;
}
.sources {
  margin-top: 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.source {
  cursor: pointer;
}
.source-text {
  max-width: 480px;
  max-height: 320px;
  overflow-y: auto;
  white-space: pre-wrap;
  font-size: 12px;
  line-height: 1.6;
}
.messages {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
}
.row {
  display: flex;
  margin-bottom: 12px;
}
.row.user {
  justify-content: flex-end;
}
.bubble {
  max-width: 75%;
  padding: 10px 14px;
  border-radius: 10px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.user .bubble {
  background: #1677ff;
  color: #fff;
}
.assistant .bubble {
  background: #f5f5f5;
}
.plain {
  white-space: pre-wrap;
}
.md :deep(p) {
  margin: 0 0 8px;
}
.md :deep(p:last-child) {
  margin-bottom: 0;
}
.md :deep(pre) {
  background: #272822;
  color: #f8f8f2;
  padding: 10px;
  border-radius: 6px;
  overflow-x: auto;
}
.waiting {
  color: #888;
}
.attachments {
  margin-top: 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.approval {
  background: #fffbe6 !important;
  border: 1px solid #ffe58f;
  max-width: 85%;
}
.approval-title {
  font-weight: 600;
  margin-bottom: 6px;
}
.plan {
  margin: 0 0 10px;
  padding-left: 20px;
}
.approval-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}
.subtasks {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
  font-size: 12px;
}
.subtasks .finished {
  color: #389e0d;
}
.subtasks .failed {
  color: #cf1322;
}
.older {
  text-align: center;
  margin-bottom: 12px;
}
.slow {
  margin-top: 6px;
  font-size: 12px;
}
.notice {
  display: flex;
  gap: 8px;
  align-items: center;
  justify-content: flex-end;
  margin: -4px 0 12px;
  font-size: 12px;
}
.notice.failed {
  color: #cf1322;
}
.notice.cancelled {
  color: #888;
}
.interrupted {
  border: 1px dashed #ff4d4f;
  background: #fff1f0;
}
.cursor {
  animation: blink 1s steps(1) infinite;
}
@keyframes blink {
  50% {
    opacity: 0;
  }
}
.alert {
  margin: 0 24px 12px;
}
.composer {
  display: flex;
  gap: 8px;
  align-items: flex-end;
  padding: 12px 24px 20px;
  border-top: 1px solid #f0f0f0;
}
</style>
