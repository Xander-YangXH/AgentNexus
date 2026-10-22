<script setup lang="ts">
import { message } from 'ant-design-vue'
import { onMounted, ref } from 'vue'
import { HttpError } from './api/http'
import ChatPanel from './components/ChatPanel.vue'
import ConversationList from './components/ConversationList.vue'
import KnowledgePanel from './components/KnowledgePanel.vue'
import UserPicker from './components/UserPicker.vue'
import { useChat } from './composables/useChat'

const chat = useChat()
const view = ref<'chat' | 'knowledge'>('chat')

const describe = (e: unknown) => (e instanceof HttpError ? e.describe() : String(e))

onMounted(() => chat.init().catch((e) => message.error(`加载失败：${describe(e)}`)))

function run(action: Promise<unknown>) {
  action.catch((e) => message.error(describe(e)))
}
</script>

<template>
  <a-layout class="layout">
    <a-layout-sider width="280" theme="light" class="sider">
      <div class="brand">AgentNexus</div>
      <UserPicker
        :users="chat.users.value"
        :user-id="chat.userId.value"
        @select="(id: string) => run(chat.selectUser(id))"
        @create="(name: string) => run(chat.createUser(name))"
      />
      <a-segmented
        v-if="chat.userId.value"
        v-model:value="view"
        block
        class="views"
        :options="[
          { value: 'chat', label: '对话' },
          { value: 'knowledge', label: '知识库' },
        ]"
      />
      <ConversationList
        v-if="chat.userId.value && view === 'chat'"
        :conversations="chat.conversations.value"
        :active-id="chat.conversationId.value"
        :show-archived="chat.showArchived.value"
        @select="(id: string) => run(chat.selectConversation(id))"
        @create="run(chat.newConversation())"
        @rename="(id: string, title: string | null) => run(chat.rename(id, title))"
        @archive="(id: string, archived: boolean) => run(chat.setArchived(id, archived))"
        @toggle-archived="(v: boolean) => run(chat.toggleArchivedView(v))"
      />
    </a-layout-sider>
    <a-layout-content class="content">
      <KnowledgePanel v-if="chat.userId.value && view === 'knowledge'" :user-id="chat.userId.value" />
      <ChatPanel
        v-else-if="chat.conversationId.value"
        :messages="chat.messages.value"
        :notices="chat.notices.value"
        :pending="chat.pending.value"
        :error="chat.error.value"
        :busy="chat.busy.value"
        :has-more="chat.hasMore.value"
        :archived="!!chat.conversation.value?.archived_at"
        @send="(text: string) => run(chat.send(text))"
        @stop="run(chat.stop())"
        @retry="(runId: string) => run(chat.retry(runId))"
        @load-older="run(chat.loadOlder())"
        @unarchive="run(chat.setArchived(chat.conversationId.value!, false))"
      />
      <a-empty v-else class="empty" :description="chat.userId.value ? '新建或选择一个对话' : '先选择或创建一个用户'" />
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
