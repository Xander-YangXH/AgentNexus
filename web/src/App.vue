<script setup lang="ts">
import { message } from 'ant-design-vue'
import { computed, defineAsyncComponent, onMounted, ref, watch } from 'vue'
import { HttpError, onUnauthorized } from './api/http'
import type { AgentKind, Attachment } from './api/types'
import ChatPanel from './components/ChatPanel.vue'
import ConversationList from './components/ConversationList.vue'
import KnowledgePanel from './components/KnowledgePanel.vue'
import LoginPanel from './components/LoginPanel.vue'
import { useChat } from './composables/useChat'
import { ROLE_LABEL } from './composables/usage'

// 用量看板和管理页按需加载：ECharts 只在打开“用量”时下载
const UsagePanel = defineAsyncComponent(() => import('./components/UsagePanel.vue'))
const AdminPanel = defineAsyncComponent(() => import('./components/AdminPanel.vue'))

const chat = useChat()
const view = ref<'chat' | 'knowledge' | 'usage' | 'admin'>('chat')
const views = computed(() => [
  { value: 'chat', label: '对话' },
  { value: 'knowledge', label: '知识库' },
  { value: 'usage', label: '用量' },
  ...(chat.me.value?.role === 'admin' ? [{ value: 'admin', label: '管理' }] : []),
])
const loginError = ref<string | null>(null)
const loggingIn = ref(false)
const ready = ref(false) // 首次检查会话之前不显示登录页，避免已登录的用户看到一闪而过的登录框

// 任何接口返回 401（会话过期、被禁用、在别处退出）：回到登录页
onUnauthorized(() => {
  if (chat.me.value) {
    chat.reset()
    message.warning('登录已失效，请重新登录')
  }
})

async function doLogin(name: string, password: string) {
  loggingIn.value = true
  loginError.value = null
  try {
    await chat.login(name, password)
    view.value = 'chat'
  } catch (e) {
    loginError.value = e instanceof HttpError && e.status === 401 ? '用户名或密码错误' : describe(e)
  } finally {
    loggingIn.value = false
  }
}
// 从知识库页面切回对话时刷新知识库列表（可能刚新建了知识库）
watch(view, (v) => {
  if (v === 'chat' && chat.userId.value) run(chat.loadKnowledgeBases())
})

const describe = (e: unknown) => (e instanceof HttpError ? e.describe() : String(e))

onMounted(() =>
  chat
    .init()
    .catch((e) => message.error(`加载失败：${describe(e)}`))
    .finally(() => (ready.value = true)),
)

function run(action: Promise<unknown>) {
  action.catch((e) => message.error(describe(e)))
}
</script>

<template>
  <LoginPanel v-if="ready && !chat.me.value" :error="loginError" :loading="loggingIn" @login="doLogin" />
  <a-layout v-else-if="chat.me.value" class="layout">
    <a-layout-sider width="280" theme="light" class="sider">
      <div class="brand">AgentNexus</div>
      <div class="me">
        <span>
          <b>{{ chat.me.value.name }}</b>
          <span class="me-meta">{{ ROLE_LABEL[chat.me.value.role] }}<template v-if="chat.me.value.department_name"> · {{ chat.me.value.department_name }}</template></span>
        </span>
        <a-button size="small" type="link" @click="run(chat.logout())">退出</a-button>
      </div>
      <a-segmented v-model:value="view" block class="views" :options="views" />
      <ConversationList
        v-if="chat.userId.value && view === 'chat'"
        :conversations="chat.conversations.value"
        :active-id="chat.conversationId.value"
        :show-archived="chat.showArchived.value"
        @select="(id: string) => run(chat.selectConversation(id))"
        @create="(agent: AgentKind) => run(chat.newConversation(agent))"
        @rename="(id: string, title: string | null) => run(chat.rename(id, title))"
        @archive="(id: string, archived: boolean) => run(chat.setArchived(id, archived))"
        @toggle-archived="(v: boolean) => run(chat.toggleArchivedView(v))"
      />
    </a-layout-sider>
    <a-layout-content class="content">
      <KnowledgePanel v-if="chat.userId.value && view === 'knowledge'" :user-id="chat.userId.value" />
      <UsagePanel v-else-if="view === 'usage'" :me="chat.me.value" />
      <AdminPanel v-else-if="view === 'admin'" />
      <ChatPanel
        v-else-if="chat.conversationId.value"
        :messages="chat.messages.value"
        :notices="chat.notices.value"
        :pending="chat.pending.value"
        :error="chat.error.value"
        :busy="chat.busy.value"
        :has-more="chat.hasMore.value"
        :archived="!!chat.conversation.value?.archived_at"
        :agent="chat.conversation.value?.agent ?? 'chat'"
        @send="(text: string) => run(chat.send(text))"
        @stop="run(chat.stop())"
        @retry="(runId: string) => run(chat.retry(runId))"
        @approve="(approved: boolean, feedback?: string) => run(chat.answerApproval(approved, feedback))"
        @download="(a: Attachment) => run(chat.download(a))"
        @load-older="run(chat.loadOlder())"
        @unarchive="run(chat.setArchived(chat.conversationId.value!, false))"
        :knowledge-bases="chat.knowledgeBases.value"
        :kb-ids="chat.conversationKbIds.value"
        @set-kbs="(ids: string[]) => run(chat.setConversationKbs(ids))"
      />
      <a-empty v-else class="empty" description="新建或选择一个对话" />
    </a-layout-content>
  </a-layout>
</template>

<style>
html,
body,
#app {
  height: 100%;
  margin: 0;
}
</style>

<style scoped>
.layout {
  height: 100%;
}
.sider {
  border-right: 1px solid #f0f0f0;
  padding: 16px;
  overflow-y: auto;
}
.views {
  margin-bottom: 16px;
}
.me {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}
.me-meta {
  margin-left: 6px;
  color: #888;
  font-size: 12px;
}
.brand {
  font-size: 18px;
  font-weight: 600;
  margin-bottom: 16px;
}
.content {
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: #fff;
}
.empty {
  margin: auto;
}
</style>
