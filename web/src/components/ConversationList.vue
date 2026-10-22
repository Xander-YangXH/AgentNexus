<script setup lang="ts">
import { ref } from 'vue'
import type { AgentKind, Conversation } from '../api/types'

defineProps<{ conversations: Conversation[]; activeId: string | null; showArchived: boolean }>()
const emit = defineEmits<{
  select: [id: string]
  create: [agent: AgentKind]
  rename: [id: string, title: string | null]
  archive: [id: string, archived: boolean]
  toggleArchived: [value: boolean]
}>()

function label(c: Conversation) {
  const title = c.title || `对话 · ${new Date(c.created_at).toLocaleString()}`
  return c.agent === 'research' ? `【调研】${title}` : title
}

// 重命名用一个对话框：留空表示清空标题（列表回退显示创建时间）
const renaming = ref<Conversation | null>(null)
const draft = ref('')
function startRename(c: Conversation) {
  renaming.value = c
  draft.value = c.title ?? ''
}
function confirmRename() {
  if (!renaming.value) return
  emit('rename', renaming.value.id, draft.value.trim() || null)
  renaming.value = null
}

function onAction(c: Conversation, key: string) {
  if (key === 'rename') startRename(c)
  else emit('archive', c.id, key === 'archive')
}
</script>

<template>
  <div>
    <!-- 对话类型建好后不能改（decisions/0022）：聊天，或深度调研（先审批计划，再并行调研、汇总成报告） -->
    <div v-if="!showArchived" class="create">
      <a-button type="primary" @click="emit('create', 'chat')">新对话</a-button>
      <a-button @click="emit('create', 'research')">深度调研</a-button>
    </div>
    <!-- 用 label 包住：点文字也能切换（开关本身是 <button>，label 会把点击转发给它） -->
    <label class="toggle">
      <a-switch size="small" :checked="showArchived" @change="(v: unknown) => emit('toggleArchived', Boolean(v))" />
      <span>显示已归档</span>
    </label>
    <a-empty v-if="!conversations.length" :description="showArchived ? '没有已归档的对话' : '还没有对话'" />
    <ul class="list">
      <li
        v-for="c in conversations"
        :key="c.id"
        :class="['item', { active: c.id === activeId }]"
        @click="emit('select', c.id)"
      >
        <span class="title" :title="label(c)">{{ label(c) }}</span>
        <a-dropdown :trigger="['click']" @click.stop>
          <a-button type="text" size="small" class="more" @click.stop>⋯</a-button>
          <template #overlay>
            <a-menu @click="({ key }: { key: string | number }) => onAction(c, String(key))">
              <a-menu-item key="rename">重命名</a-menu-item>
              <a-menu-item v-if="!c.archived_at" key="archive">归档</a-menu-item>
              <a-menu-item v-else key="unarchive">恢复</a-menu-item>
            </a-menu>
          </template>
        </a-dropdown>
      </li>
    </ul>

    <a-modal :open="!!renaming" title="重命名对话" ok-text="保存" cancel-text="取消" @ok="confirmRename" @cancel="renaming = null">
      <a-input v-model:value="draft" :maxlength="200" placeholder="留空则显示创建时间" @press-enter="confirmRename" />
    </a-modal>
  </div>
</template>

<style scoped>
.create {
  display: flex;
  gap: 8px;
}
.create > * {
  flex: 1;
}
.toggle {
  cursor: pointer;
  display: flex;
  gap: 8px;
  align-items: center;
  margin: 12px 0 4px;
  color: #888;
  font-size: 12px;
}
.list {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
}
.item {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 8px;
  border-radius: 6px;
  cursor: pointer;
}
.item:hover {
  background: #f5f5f5;
}
.item.active {
  background: #e6f4ff;
  color: #1677ff;
}
.title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.more {
  opacity: 0.6;
}
</style>
