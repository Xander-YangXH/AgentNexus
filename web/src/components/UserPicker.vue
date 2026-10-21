<script setup lang="ts">
import { ref } from 'vue'
import type { User } from '../api/types'

defineProps<{ users: User[]; userId: string | null }>()
const emit = defineEmits<{ select: [id: string]; create: [name: string] }>()
const newName = ref('')

function create() {
  const name = newName.value.trim()
  if (!name) return
  emit('create', name)
  newName.value = ''
}
</script>

<template>
  <div class="picker">
    <div class="label">当前用户（鉴权占位，阶段 5 换成登录）</div>
    <a-select
      :value="userId ?? undefined"
      placeholder="选择用户"
      style="width: 100%"
      :options="users.map((u) => ({ value: u.id, label: u.name }))"
      @change="(v: unknown) => emit('select', String(v))"
    />
    <a-input-search v-model:value="newName" placeholder="新用户名" enter-button="创建" class="create" @search="create" />
  </div>
</template>

<style scoped>
.picker {
  margin-bottom: 16px;
}
.label {
  color: #888;
  font-size: 12px;
  margin-bottom: 6px;
}
.create {
  margin-top: 8px;
}
</style>
