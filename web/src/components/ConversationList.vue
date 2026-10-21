<script setup lang="ts">
import type { Conversation } from '../api/types'

defineProps<{ conversations: Conversation[]; activeId: string | null }>()
const emit = defineEmits<{ select: [id: string]; create: [] }>()

function label(c: Conversation) {
  return c.title || `对话 · ${new Date(c.created_at).toLocaleString()}`
}
</script>

<template>
  <div>
    <a-button type="primary" block @click="emit('create')">新对话</a-button>
    <a-menu
      class="list"
      mode="inline"
      :selected-keys="activeId ? [activeId] : []"
      @click="({ key }: { key: string | number }) => emit('select', String(key))"
    >
      <a-menu-item v-for="c in conversations" :key="c.id">{{ label(c) }}</a-menu-item>
    </a-menu>
  </div>
</template>

<style scoped>
.list {
  border-inline-end: none !important;
  margin-top: 8px;
}
</style>
