<script setup lang="ts">
// 全局导航（decisions/0024）：最左侧一条窄栏，页面切换时结构不变；下方是主题切换和账号菜单
import { BarChartOutlined, BookOutlined, LogoutOutlined, MessageOutlined, SettingOutlined } from '@ant-design/icons-vue'
import { computed } from 'vue'
import type { Me } from '../api/types'
import { ROLE_LABEL } from '../composables/usage'
import { useTheme, type Preference } from '../theme/useTheme'

export type View = 'chat' | 'knowledge' | 'usage' | 'admin'

const props = defineProps<{ view: View; me: Me }>()
const emit = defineEmits<{ navigate: [view: View]; logout: [] }>()
const { preference, mode, setPreference } = useTheme()

const items = computed(() => [
  { key: 'chat' as View, label: '对话', icon: MessageOutlined },
  { key: 'knowledge' as View, label: '知识库', icon: BookOutlined },
  { key: 'usage' as View, label: '用量', icon: BarChartOutlined },
  ...(props.me.role === 'admin' ? [{ key: 'admin' as View, label: '管理', icon: SettingOutlined }] : []),
])

const THEME_OPTIONS: { key: Preference; label: string }[] = [
  { key: 'system', label: '跟随系统' },
  { key: 'light', label: '浅色' },
  { key: 'dark', label: '深色' },
]
const initial = computed(() => props.me.name.slice(0, 1).toUpperCase())
</script>

<template>
  <nav class="rail" aria-label="主导航">
    <div class="logo" title="AgentNexus">
      <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
        <path d="M12 2 21 7v10l-9 5-9-5V7z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" />
        <circle cx="12" cy="12" r="3" fill="currentColor" />
      </svg>
    </div>
    <a-tooltip v-for="item in items" :key="item.key" :title="item.label" placement="right">
      <button :class="['item', { active: view === item.key }]" :aria-label="item.label"
              :aria-current="view === item.key ? 'page' : undefined" @click="emit('navigate', item.key)">
        <component :is="item.icon" />
        <span class="item-label">{{ item.label }}</span>
      </button>
    </a-tooltip>

    <div class="spacer" />

    <a-dropdown placement="topLeft" :trigger="['click']">
      <a-tooltip title="外观" placement="right">
        <button class="item" aria-label="切换外观">
          <!-- 太阳 / 月亮：图标库里没有，内联 SVG -->
          <svg v-if="mode === 'dark'" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
            <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" fill="currentColor" />
          </svg>
          <svg v-else viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
            <circle cx="12" cy="12" r="4" fill="currentColor" />
            <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"
                  stroke="currentColor" stroke-width="2" stroke-linecap="round" />
          </svg>
        </button>
      </a-tooltip>
      <template #overlay>
        <a-menu :selected-keys="[preference]" @click="({ key }: { key: string | number }) => setPreference(key as Preference)">
          <a-menu-item v-for="o in THEME_OPTIONS" :key="o.key">{{ o.label }}</a-menu-item>
        </a-menu>
      </template>
    </a-dropdown>

    <a-dropdown placement="topLeft" :trigger="['click']">
      <button class="avatar" :aria-label="`账号：${me.name}`">{{ initial }}</button>
      <template #overlay>
        <div class="account an-card">
          <div class="account-name">{{ me.name }}</div>
          <div class="an-hint">{{ ROLE_LABEL[me.role] }}<template v-if="me.department_name"> · {{ me.department_name }}</template></div>
          <a-button block class="logout" @click="emit('logout')"><LogoutOutlined /> 退出登录</a-button>
        </div>
      </template>
    </a-dropdown>
  </nav>
</template>

<style scoped>
.rail {
  width: 64px;
  flex: none;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 14px 0;
  background: var(--an-surface);
  border-right: 1px solid var(--an-border);
}
.logo {
  width: 36px;
  height: 36px;
  display: grid;
  place-items: center;
  margin-bottom: 14px;
  border-radius: 10px;
  color: #fff;
  background: linear-gradient(135deg, var(--an-primary), #7c3aed);
}
.item {
  width: 48px;
  padding: 8px 0 6px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  border: 0;
  border-radius: 10px;
  background: transparent;
  color: var(--an-text-2);
  font-size: 18px;
  cursor: pointer;
  transition: background-color 0.15s, color 0.15s;
}
.item:hover {
  background: var(--an-surface-2);
  color: var(--an-text);
}
.item.active {
  background: var(--an-primary-soft);
  color: var(--an-primary-text);
}
.item:focus-visible,
.avatar:focus-visible {
  outline: 2px solid var(--an-primary);
  outline-offset: 2px;
}
.item-label {
  font-size: 11px;
  line-height: 1.2;
}
.spacer {
  flex: 1;
}
.avatar {
  width: 34px;
  height: 34px;
  margin-top: 8px;
  border: 0;
  border-radius: 50%;
  background: var(--an-primary-soft);
  color: var(--an-primary-text);
  font-weight: 600;
  cursor: pointer;
}
.account {
  width: 220px;
  padding: 14px;
  box-shadow: var(--an-shadow-lg);
}
.account-name {
  font-weight: 600;
}
.logout {
  margin-top: 12px;
}
</style>
