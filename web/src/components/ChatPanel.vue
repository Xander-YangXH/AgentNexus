<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import type { Message } from '../api/types'
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
}>()
const emit = defineEmits<{ send: [text: string]; stop: []; retry: [runId: string]; loadOlder: []; unarchive: [] }>()

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
    <div ref="scroller" class="messages">
      <div v-if="hasMore" class="older">
        <a-button size="small" @click="emit('loadOlder')">加载更早的消息</a-button>
      </div>
      <template v-for="m in messages" :key="m.id">
        <div :class="['row', m.role]">
          <div v-if="m.role === 'assistant'" class="bubble md" v-html="renderMarkdown(m.content)" />
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
        <div v-if="pending.status === 'waiting'" class="bubble waiting">
          <a-spin size="small" /> 等待模型开始输出… {{ waitedSeconds }} 秒
          <div v-if="waitedSeconds >= SLOW_HINT_SECONDS" class="slow">模型响应较慢，可以继续等待，或点击“停止”。</div>
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
        placeholder="输入消息，Enter 发送，Shift+Enter 换行"
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
