<script setup lang="ts">
// 应用外壳（decisions/0024）：全局导航栏 + 页面。主题（浅色 / 深色）由 token 驱动，同时作用于组件库和我们自己的样式
import { message } from 'ant-design-vue'
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { HttpError, onUnauthorized } from './api/http'
import type { AgentKind, Attachment } from './api/types'
import ChatPanel from './components/ChatPanel.vue'
import ConversationList from './components/ConversationList.vue'
import KnowledgePanel from './components/KnowledgePanel.vue'
import LoginPanel from './components/LoginPanel.vue'
import NavRail, { type View } from './components/NavRail.vue'
import { useChat } from './composables/useChat'
import { describeLoginError, describeRequestError } from './utils/errors'
import { antdTheme } from './theme/tokens'
import { provideTheme } from './theme/useTheme'

// 用量看板和管理页按需加载：ECharts 只在打开“用量”时下载
const UsagePanel = defineAsyncComponent(() => import('./components/UsagePanel.vue'))
const AdminPanel = defineAsyncComponent(() => import('./components/AdminPanel.vue'))

const { mode } = provideTheme()
const themeConfig = computed(() => antdTheme(mode.value))

const chat = useChat()
const view = ref<View>('chat')
const loginError = ref<string | null>(null)
const loggingIn = ref(false)
const ready = ref(false) // 首次检查会话之前不显示登录页，避免已登录的用户看到一闪而过的登录框

// 窄窗口（< 960px）：对话列表收进抽屉，内容区占满
const width = ref(window.innerWidth)
const onResize = () => (width.value = window.innerWidth)
const narrow = computed(() => width.value < 960)
const drawerOpen = ref(false)
onMounted(() => window.addEventListener('resize', onResize))
onBeforeUnmount(() => window.removeEventListener('resize', onResize))

// 任何接口返回 401（会话过期、被禁用、在别处退出）：回到登录页
onUnauthorized(() => {
  if (chat.me.value) {
    chat.reset()
    message.warning('登录已失效，请重新登录')
  }
})

const describe = (e: unknown) => (e instanceof HttpError ? (describeRequestError(e.code) ?? e.describe()) : String(e))

function run(action: Promise<unknown>) {
  action.catch((e) => message.error(describe(e)))
}

async function doLogin(name: string, password: string) {
  loggingIn.value = true
  loginError.value = null
  try {
    await chat.login(name, password)
    view.value = 'chat'
  } catch (e) {
    loginError.value = (e instanceof HttpError && describeLoginError(e.status, e.body?.details)) || describe(e)
  } finally {
    loggingIn.value = false
  }
}

function selectConversation(id: string) {
  drawerOpen.value = false
  run(chat.selectConversation(id))
}

function createConversation(agent: AgentKind) {
  drawerOpen.value = false
  run(chat.newConversation(agent))
}

// 从知识库页面切回对话时刷新知识库列表（可能刚新建或共享了知识库）
watch(view, (v) => {
  if (v === 'chat' && chat.userId.value) run(chat.loadKnowledgeBases())
})

onMounted(() =>
  chat
    .init()
    .catch((e) => message.error(`加载失败：${describe(e)}`))
    .finally(() => (ready.value = true)),
)
</script>

<template>
  <a-config-provider :theme="themeConfig">
    <LoginPanel v-if="ready && !chat.me.value" :error="loginError" :loading="loggingIn" @login="doLogin" />
    <div v-else-if="chat.me.value" class="shell">
      <NavRail :view="view" :me="chat.me.value" @navigate="(v: View) => (view = v)" @logout="run(chat.logout())" />

      <template v-if="view === 'chat'">
        <aside v-if="!narrow" class="sidebar">
          <ConversationList
            :conversations="chat.conversations.value"
            :active-id="chat.conversationId.value"
            :show-archived="chat.showArchived.value"
            @select="selectConversation"
            @create="createConversation"
            @rename="(id: string, title: string | null) => run(chat.rename(id, title))"
            @archive="(id: string, archived: boolean) => run(chat.setArchived(id, archived))"
            @delete="(id: string) => run(chat.deleteConversation(id))"
            @toggle-archived="(v: boolean) => run(chat.toggleArchivedView(v))"
          />
        </aside>
        <a-drawer v-else :open="drawerOpen" placement="left" :width="300" :closable="false" :body-style="{ padding: 0 }"
                  @close="drawerOpen = false">
          <ConversationList
            :conversations="chat.conversations.value"
            :active-id="chat.conversationId.value"
            :show-archived="chat.showArchived.value"
            @select="selectConversation"
            @create="createConversation"
            @rename="(id: string, title: string | null) => run(chat.rename(id, title))"
            @archive="(id: string, archived: boolean) => run(chat.setArchived(id, archived))"
            @delete="(id: string) => run(chat.deleteConversation(id))"
            @toggle-archived="(v: boolean) => run(chat.toggleArchivedView(v))"
          />
        </a-drawer>
      </template>

      <main class="main">
        <KnowledgePanel v-if="view === 'knowledge'" :user-id="chat.me.value.id" />
        <UsagePanel v-else-if="view === 'usage'" :me="chat.me.value" />
        <AdminPanel v-else-if="view === 'admin'" />
        <ChatPanel
          v-else-if="chat.conversationId.value"
          :title="chat.conversation.value?.title ?? null"
          :agent="chat.conversation.value?.agent ?? 'chat'"
          :messages="chat.messages.value"
          :notices="chat.notices.value"
          :pending="chat.pending.value"
          :error="chat.error.value"
          :busy="chat.busy.value"
          :has-more="chat.hasMore.value"
          :archived="!!chat.conversation.value?.archived_at"
          :knowledge-bases="chat.knowledgeBases.value"
          :kb-ids="chat.conversationKbIds.value"
          :user-id="chat.me.value.id"
          :show-menu="narrow"
          @menu="drawerOpen = true"
          @send="(text: string) => run(chat.send(text))"
          @stop="run(chat.stop())"
          @retry="(runId: string) => run(chat.retry(runId))"
          @approve="(approved: boolean, feedback?: string) => run(chat.answerApproval(approved, feedback))"
          @download="(a: Attachment) => run(chat.download(a))"
          @load-older="run(chat.loadOlder())"
          @unarchive="run(chat.setArchived(chat.conversationId.value!, false))"
          @set-kbs="(ids: string[]) => run(chat.setConversationKbs(ids))"
        />
        <div v-else class="welcome">
          <a-button v-if="narrow" class="welcome-menu" @click="drawerOpen = true">对话列表</a-button>
          <h2>开始一段对话</h2>
          <p class="an-muted">普通对话可以关联知识库、使用代码沙盒和网页抓取；深度调研会先给出计划，确认后并行调研并生成报告。</p>
          <div class="welcome-actions">
            <a-button type="primary" size="large" @click="createConversation('chat')">新对话</a-button>
            <a-button size="large" @click="createConversation('research')">深度调研</a-button>
          </div>
        </div>
      </main>
    </div>
  </a-config-provider>
</template>

<style scoped>
.shell {
  display: flex;
  height: 100%;
  background: var(--an-bg);
}
.sidebar {
  width: 280px;
  flex: none;
  border-right: 1px solid var(--an-border);
  background: var(--an-surface);
}
.main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: var(--an-bg);
}
.welcome {
  margin: auto;
  max-width: 520px;
  padding: 24px;
  text-align: center;
}
.welcome h2 {
  font-size: 22px;
  font-weight: 600;
  margin: 0 0 8px;
}
.welcome-actions {
  display: flex;
  gap: 12px;
  justify-content: center;
  margin-top: 20px;
}
.welcome-menu {
  margin-bottom: 24px;
}
</style>
