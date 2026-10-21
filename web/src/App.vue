<script setup lang="ts">
import { message } from 'ant-design-vue'
import { onMounted } from 'vue'
import ChatPanel from './components/ChatPanel.vue'
import ConversationList from './components/ConversationList.vue'
import UserPicker from './components/UserPicker.vue'
import { useChat } from './composables/useChat'

const chat = useChat()

onMounted(() => chat.init().catch((e) => message.error(`加载失败：${String(e)}`)))

function run(action: Promise<unknown>) {
  action.catch((e) => message.error(String(e)))
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
      <ConversationList
        v-if="chat.userId.value"
        :conversations="chat.conversations.value"
        :active-id="chat.conversationId.value"
        @select="(id: string) => run(chat.selectConversation(id))"
        @create="run(chat.newConversation())"
      />
    </a-layout-sider>
    <a-layout-content class="content">
      <ChatPanel
        v-if="chat.conversationId.value"
        :messages="chat.messages.value"
        :notices="chat.notices.value"
        :pending="chat.pending.value"
        :error="chat.error.value"
        :busy="chat.busy.value"
        @send="(text: string) => run(chat.send(text))"
        @stop="run(chat.stop())"
        @retry="(runId: string) => run(chat.retry(runId))"
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
