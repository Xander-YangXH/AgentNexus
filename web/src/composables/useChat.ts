import { computed, ref, shallowRef } from 'vue'
import { api, authHeaders, HttpError, setCurrentUser } from '../api/http'
import { streamRunEvents } from '../api/sse'
import type { Conversation, KnowledgeBase, Message, Run, User } from '../api/types'
import { applyRunEvent, isActive, newPending, type PendingReply } from './pendingReply'
import { mergeMessages } from './messagePages'
import { computeNotices } from './runNotices'

const USER_KEY = 'agentnexus.userId'

function readStoredUser(): string | null {
  try {
    return localStorage.getItem(USER_KEY)
  } catch {
    return null // 隐私模式等情况下 localStorage 可能不可用
  }
}
function storeUser(id: string | null) {
  try {
    if (id) localStorage.setItem(USER_KEY, id)
    else localStorage.removeItem(USER_KEY)
  } catch {
    /* 只是“记住上次选的用户”的便利功能，失败不影响使用 */
  }
}

export function useChat() {
  const users = ref<User[]>([])
  const userId = ref<string | null>(null)
  const conversations = ref<Conversation[]>([])
  const showArchived = ref(false)
  const conversationId = ref<string | null>(null)
  // 当前对话对象可能不在列表里（例如刚归档、而列表显示的是未归档的），所以单独保存
  const conversation = ref<Conversation | null>(null)
  const messages = ref<Message[]>([])
  const hasMore = ref(false)
  const runs = ref<Run[]>([])
  const pending = ref<PendingReply | null>(null)
  const error = ref<string | null>(null)
  const sending = ref(false)
  // 知识库（decisions/0018）：当前用户的全部知识库，以及当前对话关联了哪些
  const knowledgeBases = ref<KnowledgeBase[]>([])
  const conversationKbIds = ref<string[]>([])
  // 当前订阅的事件流；切换对话或用户时中止，避免旧流把 token 写进新对话
  const streamAbort = shallowRef<AbortController | null>(null)

  const busy = computed(() => sending.value || isActive(pending.value))
  const notices = computed(() => computeNotices(messages.value, runs.value))

  // 以数据库为准刷新：最近一页消息 + run 一起拉；与已加载的更早消息合并，不丢用户翻出来的旧消息
  async function reload(cid: string, { reset = false } = {}) {
    const [page, r] = await Promise.all([api.listMessagesPage(cid), api.listRuns(cid)])
    if (conversationId.value !== cid) return
    messages.value = reset ? page.items : mergeMessages(messages.value, page.items)
    if (reset) hasMore.value = page.hasMore
    runs.value = r
  }

  async function loadOlder() {
    const cid = conversationId.value
    if (!cid || !messages.value.length) return
    const page = await api.listMessagesPage(cid, messages.value[0].seq)
    if (conversationId.value !== cid) return
    messages.value = mergeMessages(messages.value, page.items)
    hasMore.value = page.hasMore
  }

  async function loadConversations() {
    conversations.value = await api.listConversations(showArchived.value)
  }

  async function toggleArchivedView(value: boolean) {
    showArchived.value = value
    await loadConversations()
  }

  async function rename(cid: string, title: string | null) {
    const c = await api.updateConversation(cid, { title })
    replaceConversation(c)
  }

  async function setArchived(cid: string, archived: boolean) {
    const c = await api.updateConversation(cid, { archived })
    if (conversationId.value === cid) conversation.value = c
    await loadConversations() // 归档后从当前列表消失 / 恢复后回到未归档列表
  }

  function replaceConversation(c: Conversation) {
    conversations.value = conversations.value.map((x) => (x.id === c.id ? c : x))
    if (conversationId.value === c.id) conversation.value = c
  }

  function stopStream() {
    streamAbort.value?.abort()
    streamAbort.value = null
  }

  async function init() {
    users.value = await api.listUsers()
    const stored = readStoredUser()
    if (stored && users.value.some((u) => u.id === stored)) await selectUser(stored)
  }

  async function createUser(name: string) {
    const u = await api.createUser(name)
    users.value = [...users.value, u]
    await selectUser(u.id)
  }

  async function selectUser(id: string) {
    stopStream()
    userId.value = id
    setCurrentUser(id)
    storeUser(id)
    conversationId.value = null
    conversation.value = null
    messages.value = []
    runs.value = []
    pending.value = null
    showArchived.value = false
    conversationKbIds.value = []
    await Promise.all([loadConversations(), loadKnowledgeBases()])
    if (conversations.value.length) await selectConversation(conversations.value[0].id)
  }

  async function newConversation() {
    const c = await api.createConversation(null)
    conversations.value = [c, ...conversations.value]
    await selectConversation(c.id)
  }

  async function selectConversation(id: string) {
    stopStream()
    conversationId.value = id
    conversation.value = conversations.value.find((c) => c.id === id) ?? null
    messages.value = []
    hasMore.value = false
    pending.value = null
    error.value = null
    conversationKbIds.value = []
    const [, kbs] = await Promise.all([reload(id, { reset: true }), api.getConversationKnowledgeBases(id)])
    if (conversationId.value === id) conversationKbIds.value = kbs.knowledge_base_ids
    // 刷新页面或切回来时，这个对话可能还有正在生成的回复：找回来并重新订阅。
    // 事件流从头回放（decisions/0007），所以已经生成的部分会立刻补齐。
    const active = await api.activeRun(id)
    // 计时从服务端创建 run 的时间算起，而不是从刷新页面的这一刻：用户关心的是“模型已经让我等了多久”
    if (active && conversationId.value === id) follow(active.id, id, Date.parse(active.created_at))
  }

  async function send(content: string) {
    if (!conversationId.value || !content.trim()) return
    const cid = conversationId.value
    sending.value = true
    error.value = null
    try {
      const run = await api.sendMessage(cid, content)
      // 用户消息已落库：重新拉取，而不是在前端手动拼一条（以数据库为准）；
      // 对话列表也刷新：最近活动时间变了（排到最前），第一条消息还可能自动成了标题
      await Promise.all([reload(cid), loadConversations()])
      follow(run.id, cid, Date.parse(run.created_at))
    } catch (e) {
      error.value =
        e instanceof HttpError && e.status === 409 ? '上一条回复还在生成中，请稍候' : `发送失败：${e instanceof HttpError ? e.describe() : String(e)}`
    } finally {
      sending.value = false
    }
  }

  function follow(runId: string, cid: string, startedAt: number) {
    stopStream()
    const controller = new AbortController()
    streamAbort.value = controller
    pending.value = newPending(runId, startedAt)

    streamRunEvents(runId, {
      headers: authHeaders(),
      signal: controller.signal,
      onEvent: (event) => {
        if (pending.value?.runId === runId) pending.value = applyRunEvent(pending.value, event)
      },
    })
      .then(async (terminal) => {
        if (conversationId.value !== cid) return
        await reload(cid) // 完成：用库里的最终回复替换临时回复；失败/取消：拿到 run 的终态，显示原因与重试入口
        if (terminal.type === 'run.completed') pending.value = null
        // 失败/取消时保留 pending，界面继续显示已收到的半截文本
      })
      .catch((e) => {
        if (controller.signal.aborted) return // 主动切换对话，不算错误
        error.value = `与服务端的连接中断：${e instanceof HttpError ? e.describe() : String(e)}`
      })
  }

  async function loadKnowledgeBases() {
    knowledgeBases.value = await api.listKnowledgeBases()
  }

  async function setConversationKbs(ids: string[]) {
    const cid = conversationId.value
    if (!cid) return
    const r = await api.setConversationKnowledgeBases(cid, ids)
    if (conversationId.value === cid) conversationKbIds.value = r.knowledge_base_ids
  }

  async function stop() {
    const runId = pending.value?.runId
    if (!runId || !isActive(pending.value)) return
    try {
      // 200（排队中已取消）或 202（执行中已发信号）：结果以随后到达的 run.cancelled 事件为准
      await api.cancelRun(runId)
    } catch (e) {
      if (!(e instanceof HttpError && e.status === 409)) error.value = `停止失败：${e instanceof HttpError ? e.describe() : String(e)}` // 409：恰好已经结束
    }
  }

  async function retry(runId: string) {
    const cid = conversationId.value
    if (!cid) return
    error.value = null
    try {
      const run = await api.retryRun(runId)
      await reload(cid)
      follow(run.id, cid, Date.parse(run.created_at))
    } catch (e) {
      error.value = e instanceof HttpError && e.status === 409 ? '这条消息现在不能重试' : `重试失败：${e instanceof HttpError ? e.describe() : String(e)}`
    }
  }

  return {
    users,
    userId,
    conversations,
    showArchived,
    conversation,
    conversationId,
    hasMore,
    loadOlder,
    toggleArchivedView,
    rename,
    setArchived,
    messages,
    knowledgeBases,
    conversationKbIds,
    loadKnowledgeBases,
    setConversationKbs,
    notices,
    pending,
    error,
    busy,
    init,
    createUser,
    selectUser,
    newConversation,
    selectConversation,
    send,
    stop,
    retry,
  }
}
