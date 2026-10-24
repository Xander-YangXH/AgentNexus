<script setup lang="ts">
// 登录（decisions/0023、0024）。左侧介绍产品能做什么，右侧登录表单；账号由管理员创建，没有自助注册
import { ApartmentOutlined, ExperimentOutlined, ReadOutlined, SafetyCertificateOutlined } from '@ant-design/icons-vue'
import { ref } from 'vue'

const props = defineProps<{ error: string | null; loading: boolean }>()
const emit = defineEmits<{ login: [name: string, password: string] }>()
const name = ref('')
const password = ref('')

const FEATURES = [
  { icon: ReadOutlined, title: '有据可查的问答', text: '关联知识库后，回答逐句标注引用，悬停即可查看原文' },
  { icon: ApartmentOutlined, title: '知识图谱', text: '从文档自动抽取实体与关系，可视化浏览并溯源' },
  { icon: ExperimentOutlined, title: '深度调研', text: '先确认计划，再并行调研、汇总成可下载的报告' },
  { icon: SafetyCertificateOutlined, title: '部门隔离', text: '知识库可在部门内共享，数据库行级安全兜底' },
]

function submit() {
  if (!name.value.trim() || !password.value || props.loading) return
  emit('login', name.value.trim(), password.value)
}
</script>

<template>
  <div class="login">
    <section class="brand">
      <div class="brand-inner">
        <div class="logo">
          <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
            <path d="M12 2 21 7v10l-9 5-9-5V7z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" />
            <circle cx="12" cy="12" r="3" fill="currentColor" />
          </svg>
          AgentNexus
        </div>
        <h1>企业知识库与 Agent 平台</h1>
        <p class="lead">把文档变成可以对话、可以溯源、可以协作的知识。</p>
        <ul class="features">
          <li v-for="f in FEATURES" :key="f.title">
            <span class="f-icon"><component :is="f.icon" /></span>
            <div>
              <div class="f-title">{{ f.title }}</div>
              <div class="f-text">{{ f.text }}</div>
            </div>
          </li>
        </ul>
      </div>
    </section>

    <section class="form-side">
      <form class="form" @submit.prevent="submit">
        <h2>登录</h2>
        <p class="an-muted">使用管理员为你创建的账号</p>
        <label class="field">
          <span class="field-label">用户名</span>
          <a-input v-model:value="name" size="large" autocomplete="username" @press-enter="submit" />
        </label>
        <label class="field">
          <span class="field-label">密码</span>
          <a-input-password v-model:value="password" size="large" autocomplete="current-password" @press-enter="submit" />
        </label>
        <a-alert v-if="error" type="error" :message="error" show-icon class="alert" />
        <a-button type="primary" size="large" block html-type="submit" :loading="loading">登录</a-button>
      </form>
    </section>
  </div>
</template>

<style scoped>
.login {
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr);
  min-height: 100vh;
  background: var(--an-bg);
}
.brand {
  display: flex;
  align-items: center;
  padding: 48px;
  color: #fff;
  /* 品牌区固定用深靛蓝渐变：浅色、深色模式下都一样 */
  background: radial-gradient(circle at 20% 20%, #6366f1 0%, transparent 55%),
    linear-gradient(140deg, #312e81 0%, #1e1b4b 60%, #0f0d2e 100%);
}
.brand-inner {
  max-width: 460px;
  margin: 0 auto;
}
.logo {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  font-size: 18px;
  font-weight: 600;
  margin-bottom: 40px;
}
h1 {
  margin: 0 0 10px;
  font-size: 30px;
  font-weight: 600;
  line-height: 1.3;
  color: #fff;
}
.lead {
  margin: 0 0 36px;
  color: rgba(255, 255, 255, 0.72);
  font-size: 15px;
}
.features {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 18px;
}
.features li {
  display: flex;
  gap: 14px;
}
.f-icon {
  flex: none;
  width: 36px;
  height: 36px;
  display: grid;
  place-items: center;
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.1);
  font-size: 17px;
}
.f-title {
  font-weight: 600;
}
.f-text {
  color: rgba(255, 255, 255, 0.65);
  font-size: 13px;
}
.form-side {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 32px;
}
.form {
  width: 100%;
  max-width: 360px;
}
.form h2 {
  margin: 0 0 4px;
  font-size: 22px;
  font-weight: 600;
}
.form > p {
  margin: 0 0 28px;
}
.field {
  display: block;
  margin-bottom: 18px;
}
/* 用类名而不是 .field > span：密码框组件的最外层也是 span，会被误伤成 block（第一版这样写，密码框高度翻倍、眼睛图标掉到下一行） */
.field-label {
  display: block;
  margin-bottom: 6px;
  font-size: 13px;
  font-weight: 500;
}
.alert {
  margin-bottom: 16px;
}
@media (max-width: 860px) {
  .login {
    grid-template-columns: 1fr;
  }
  .brand {
    padding: 32px 24px;
  }
  .features,
  .lead {
    display: none;
  }
  .logo {
    margin-bottom: 12px;
  }
  h1 {
    font-size: 22px;
    margin: 0;
  }
}
</style>
