<script setup lang="ts">
// 登录（decisions/0023）。账号由管理员创建（或用 python -m agentnexus.cli create-user），没有自助注册
import { ref } from 'vue'

const props = defineProps<{ error: string | null; loading: boolean }>()
const emit = defineEmits<{ login: [name: string, password: string] }>()
const name = ref('')
const password = ref('')

function submit() {
  if (!name.value.trim() || !password.value || props.loading) return
  emit('login', name.value.trim(), password.value)
}
</script>

<template>
  <div class="login">
    <a-card title="登录 AgentNexus" class="card">
      <a-form layout="vertical" @submit.prevent="submit">
        <a-form-item label="用户名">
          <a-input v-model:value="name" autocomplete="username" @press-enter="submit" />
        </a-form-item>
        <a-form-item label="密码">
          <a-input-password v-model:value="password" autocomplete="current-password" @press-enter="submit" />
        </a-form-item>
        <a-alert v-if="error" type="error" :message="error" show-icon class="alert" />
        <a-button type="primary" block :loading="loading" @click="submit">登录</a-button>
      </a-form>
    </a-card>
  </div>
</template>

<style scoped>
.login {
  display: flex;
  justify-content: center;
  align-items: center;
  height: 100vh;
  background: #f5f5f5;
}
.card {
  width: 360px;
}
.alert {
  margin-bottom: 12px;
}
</style>
