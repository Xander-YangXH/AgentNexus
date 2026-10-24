<script setup lang="ts">
// 对话列表（decisions/0024）：按最近活动分组（今天 / 昨天 / 最近 7 天 / 更早），可搜索；调研对话带图标
import { EllipsisOutlined, ExperimentOutlined, MessageOutlined, PlusOutlined, SearchOutlined } from '@ant-design/icons-vue'
import { Modal } from 'ant-design-vue'
import { computed, ref } from 'vue'
import type { AgentKind, Conversation } from '../api/types'
import { conversationLabel, groupByRecency, matches } from '../composables/conversations'

const props = defineProps<{ conversations: Conversation[]; activeId: string | null; showArchived: boolean }>()
const emit = defineEmits<{
  select: [id: string]
  create: [agent: AgentKind]
  rename: [id: string, title: string | null]
  archive: [id: string, archived: boolean]
  delete: [id: string]
  toggleArchived: [value: boolean]
}>()

const query = ref('')
const groups = computed(() => groupByRecency(props.conversations.filter((c) => matches(c, query.value))))

// 重命名用一个对话框：留空表示清空标题（列表回退显示创建日期）
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
  else if (key === 'delete') confirmDelete(c)
  else emit('archive', c.id, key === 'archive')
}

// 删除不可恢复（decisions/0028），所以要确认；只想收起来用“归档”
function confirmDelete(c: Conversation) {
  Modal.confirm({
    title: `删除「${conversationLabel(c)}」？`,
    content: '消息、生成的文件和调研报告会一起删除，无法恢复。只是不想看到它，可以用“归档”。',
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    onOk: () => emit('delete', c.id),
  })
}
</script>

<template>
  <div class="list-panel">
    <div class="head">
      <div class="title">{{ showArchived ? '已归档' : '对话' }}</div>
      <!-- 对话类型建好后不能改（decisions/0022）：普通对话，或深度调研（先确认计划，再并行调研、汇总成报告） -->
      <div v-if="!showArchived" class="create">
        <a-button type="primary" block @click="emit('create', 'chat')"><PlusOutlined /> 新对话</a-button>
        <a-tooltip title="先给出调研计划请你确认，再并行执行子任务、汇总成可下载的报告" placement="bottom">
          <a-button block @click="emit('create', 'research')"><ExperimentOutlined /> 深度调研</a-button>
        </a-tooltip>
      </div>
      <a-input v-model:value="query" placeholder="搜索对话" allow-clear class="search">
        <template #prefix><SearchOutlined class="an-hint" /></template>
      </a-input>
    </div>

    <div class="scroll">
      <div v-if="!groups.length" class="empty an-hint">
        {{ query ? '没有匹配的对话' : showArchived ? '没有已归档的对话' : '还没有对话，点上面的按钮开始' }}
      </div>
      <section v-for="g in groups" :key="g.label" class="group">
        <div class="group-label">{{ g.label }}</div>
        <div
          v-for="c in g.items"
          :key="c.id"
          :class="['item', { active: c.id === activeId }]"
          role="button"
          tabindex="0"
          @click="emit('select', c.id)"
          @keydown.enter="emit('select', c.id)"
        >
          <ExperimentOutlined v-if="c.agent === 'research'" class="kind research" />
          <MessageOutlined v-else class="kind" />
          <span class="label" :title="conversationLabel(c)">{{ conversationLabel(c) }}</span>
          <a-dropdown :trigger="['click']" @click.stop>
            <a-button type="text" size="small" class="more" aria-label="更多操作" @click.stop><EllipsisOutlined /></a-button>
            <template #overlay>
              <a-menu @click="({ key }: { key: string | number }) => onAction(c, String(key))">
                <a-menu-item key="rename">重命名</a-menu-item>
                <a-menu-item v-if="!c.archived_at" key="archive">归档</a-menu-item>
                <a-menu-item v-else key="unarchive">恢复</a-menu-item>
                <a-menu-item key="delete" danger>删除</a-menu-item>
              </a-menu>
            </template>
          </a-dropdown>
        </div>
      </section>
    </div>

    <label class="toggle">
      <a-switch size="small" :checked="showArchived" @change="(v: unknown) => emit('toggleArchived', Boolean(v))" />
      <span>显示已归档</span>
    </label>

    <a-modal :open="!!renaming" title="重命名对话" ok-text="保存" cancel-text="取消" @ok="confirmRename" @cancel="renaming = null">
      <a-input v-model:value="draft" :maxlength="200" placeholder="留空则显示创建日期" @press-enter="confirmRename" />
    </a-modal>
  </div>
</template>

<style scoped>
.list-panel {
  height: 100%;
  display: flex;
  flex-direction: column;
}
.head {
  padding: 18px 16px 10px;
}
.title {
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 12px;
}
.create {
  display: flex;
  gap: 8px;
}
.create > * {
  flex: 1;
}
.search {
  margin-top: 12px;
}
.scroll {
  flex: 1;
  overflow-y: auto;
  padding: 0 8px 8px;
}
.empty {
  padding: 24px 12px;
  text-align: center;
}
.group-label {
  padding: 12px 10px 4px;
  font-size: 12px;
  color: var(--an-text-3);
}
.item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 8px 7px 10px;
  border-radius: 8px;
  cursor: pointer;
  color: var(--an-text);
  transition: background-color 0.12s;
}
.item:hover {
  background: var(--an-surface-2);
}
.item:focus-visible {
  outline: 2px solid var(--an-primary);
}
.item.active {
  background: var(--an-primary-soft);
  color: var(--an-primary-text);
}
.kind {
  color: var(--an-text-3);
  font-size: 13px;
}
.kind.research {
  color: var(--an-primary);
}
.label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.more {
  opacity: 0;
  color: var(--an-text-2);
}
.item:hover .more,
.item.active .more {
  opacity: 1;
}
.toggle {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 12px 16px;
  border-top: 1px solid var(--an-border);
  color: var(--an-text-2);
  font-size: 12px;
  cursor: pointer;
}
</style>
