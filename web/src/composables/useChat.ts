import { computed, ref, shallowRef } from 'vue'
import { api, authHeaders, HttpError, setCurrentUser } from '../api/http'
import { streamRunEvents } from '../api/sse'
import type { Conversation, Message, User } from '../api/types'
import { applyRunEvent, newPending, type PendingReply } from './pendingReply'

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
  const conversationId = ref<string | null>(null)
  const messages = ref<Message[]>([])
  const pending = ref<PendingReply | null>(null)
  const error = ref<string | null>(null)
  const sending = ref(false)
  // 当前订阅的事件流；切换对话或用户时中止，避免旧流把 token 写进新对话
  const streamAbort = shallowRef<AbortController | null>(null)

  const busy = computed(
    () => sending.value || pending.value?.status === 'waiting' || pending.value?.status === 'streaming',
  )

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
    messages.value = []
    pending.value = null
    conversations.value = await api.listConversations()
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
    pending.value = null
    error.value = null
    messages.value = await api.listMessages(id)
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
      // 用户消息已落库：重新拉取列表，而不是在前端手动拼一条（以数据库为准）
      messages.value = await api.listMessages(cid)
      follow(run.id, cid, Date.parse(run.created_at))
    } catch (e) {
      error.value =
        e instanceof HttpError && e.status === 409 ? '上一条回复还在生成中，请稍候' : `发送失败：${String(e)}`
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
        if (terminal.type === 'run.completed') {
          messages.value = await api.listMessages(cid) // 用库里的最终回复替换临时回复
          pending.value = null
        }
        // 失败时保留 pending（status=failed），界面显示半截文本 + “生成中断”
      })
      .catch((e) => {
        if (controller.signal.aborted) return // 主动切换对话，不算错误
        error.value = `与服务端的连接中断：${String(e)}`
      })
  }

  return {
    users,
    userId,
    conversations,
    conversationId,
    messages,
    pending,
    error,
    busy,
    init,
    createUser,
    selectUser,
    newConversation,
    selectConversation,
    send,
  }
}
