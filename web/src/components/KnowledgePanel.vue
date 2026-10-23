<script setup lang="ts">
// 知识库页面：管理文档 + 检索测试（看召回了什么、分数多少、每步多久）。数据以后端为准，这里不做本地缓存
import { message } from 'ant-design-vue'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api, HttpError } from '../api/http'
import type { Chunk, KnowledgeBase, KnowledgeDocument, SearchResult } from '../api/types'
import { formatSize, formatTimings, needsPolling, STATUS_TAG } from '../composables/knowledge'
import GraphPanel from './GraphPanel.vue'
import { describeDocumentError, describeRequestError } from '../utils/errors'

const props = defineProps<{ userId: string }>()

const POLL_MS = 2000

const kbs = ref<KnowledgeBase[]>([])
const kbId = ref<string | null>(null)
const newKbName = ref('')
const docs = ref<KnowledgeDocument[]>([])
const uploading = ref(false)

const chunksOf = ref<KnowledgeDocument | null>(null)
const chunks = ref<Chunk[]>([])

const tab = ref<'docs' | 'graph'>('docs')
const query = ref('')
const topK = ref(5)
const rerank = ref(true)
const searching = ref(false)
const result = ref<SearchResult | null>(null)

function showError(e: unknown) {
  if (e instanceof HttpError) message.error(describeRequestError(e.code) ?? e.describe())
  else message.error(String(e))
}

// 部门共享给我的知识库（decisions/0023）：只读——能看文档、检索、浏览图谱，不能上传、重新处理、构建、改共享
const current = computed(() => kbs.value.find((k) => k.id === kbId.value) ?? null)
const readonly = computed(() => !!current.value && current.value.user_id !== props.userId)

async function setShared(shared: boolean) {
  if (!current.value) return
  try {
    await api.setKnowledgeBaseVisibility(current.value.id, shared ? 'department' : 'private')
    await loadKbs()
    message.success(shared ? '已共享给本部门（只读）' : '已取消共享')
  } catch (e) {
    showError(e)
  }
}

async function loadKbs() {
  kbs.value = await api.listKnowledgeBases()
  if (!kbs.value.some((k) => k.id === kbId.value)) kbId.value = kbs.value[0]?.id ?? null
}

async function createKb() {
  const name = newKbName.value.trim()
  if (!name) return
  try {
    const kb = await api.createKnowledgeBase(name)
    newKbName.value = ''
    await loadKbs()
    kbId.value = kb.id
  } catch (e) {
    showError(e)
  }
}

// 轮询：有文档在排队或处理时每 2 秒刷新一次状态，全部结束就停。切换知识库、离开页面时也停
let timer: ReturnType<typeof setTimeout> | null = null
function stopPolling() {
  if (timer) clearTimeout(timer)
  timer = null
}
async function loadDocs() {
  stopPolling()
  const id = kbId.value
  if (!id) {
    docs.value = []
    return
  }
  const list = await api.listDocuments(id)
  if (id !== kbId.value) return // 请求期间切换了知识库：丢弃过期结果
  docs.value = list
  if (needsPolling(list)) timer = setTimeout(() => loadDocs().catch(showError), POLL_MS)
}

async function onFile(file: File) {
  if (!kbId.value) return false
  uploading.value = true
  try {
    await api.uploadDocument(kbId.value, file)
    message.success(`已上传 ${file.name}，后台处理中`)
    await loadDocs()
  } catch (e) {
    showError(e)
  } finally {
    uploading.value = false
  }
  return false // 阻止组件自己再上传一遍
}

async function reprocess(doc: KnowledgeDocument) {
  try {
    await api.reprocessDocument(doc.id)
    await loadDocs()
  } catch (e) {
    showError(e)
  }
}

async function openChunks(doc: KnowledgeDocument) {
  chunksOf.value = doc
  chunks.value = []
  try {
    chunks.value = await api.listChunks(doc.id)
  } catch (e) {
    showError(e)
  }
}

async function runSearch() {
  const q = query.value.trim()
  if (!q || !kbId.value) return
  searching.value = true
  try {
    result.value = await api.search([kbId.value], q, topK.value, rerank.value)
  } catch (e) {
    showError(e)
  } finally {
    searching.value = false
  }
}

watch(kbId, () => {
  result.value = null
  loadDocs().catch(showError)
})
watch(
  () => props.userId,
  () => {
    kbId.value = null
    loadKbs().catch(showError)
  },
  { immediate: true },
)
onBeforeUnmount(stopPolling)

const columns = [
  { title: '文件', dataIndex: 'filename', key: 'filename', ellipsis: true },
  { title: '大小', key: 'size', width: 90 },
  { title: '状态', key: 'status', width: 230 },
  { title: '分块', dataIndex: 'chunk_count', key: 'chunks', width: 70 },
  { title: '', key: 'actions', width: 140 },
]
</script>

<template>
  <div class="knowledge">
    <div class="toolbar">
      <a-select
        v-model:value="kbId"
        placeholder="选择知识库"
        style="width: 220px"
        :options="kbs.map((k) => ({ value: k.id, label: k.user_id === userId ? k.name : `${k.name}（部门共享）` }))"
      />
      <template v-if="current && !readonly">
        <a-switch :checked="current.visibility === 'department'" checked-children="部门共享" un-checked-children="私有"
                  @change="(v: unknown) => setShared(Boolean(v))" />
      </template>
      <a-tag v-else-if="readonly" color="blue">部门共享 · 只读</a-tag>
      <a-input-search v-model:value="newKbName" placeholder="新知识库名称" enter-button="新建" style="width: 260px" @search="createKb" />
      <a-upload v-if="kbId && !readonly" :before-upload="onFile" :show-upload-list="false" accept=".txt,.md,.markdown,.pdf,.docx">
        <a-button type="primary" :loading="uploading">上传文档</a-button>
      </a-upload>
    </div>

    <a-tabs v-if="kbId" v-model:active-key="tab">
      <a-tab-pane key="docs" tab="文档与检索">
        <a-table :columns="columns" :data-source="docs" row-key="id" size="small" :pagination="false" class="docs">
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'size'">{{ formatSize(record.size_bytes) }}</template>
            <template v-else-if="column.key === 'status'">
              <a-tag :color="STATUS_TAG[record.status as keyof typeof STATUS_TAG].color">
                {{ STATUS_TAG[record.status as keyof typeof STATUS_TAG].label }}
              </a-tag>
              <span v-if="record.status === 'failed'" class="error">{{ describeDocumentError(record.error_code) }}</span>
            </template>
            <template v-else-if="column.key === 'actions'">
              <a-button v-if="record.status === 'ready'" size="small" type="link" @click="openChunks(record)">查看分块</a-button>
              <a-button v-if="record.status === 'failed' && !readonly" size="small" type="link" @click="reprocess(record)">重新处理</a-button>
            </template>
          </template>
        </a-table>

        <a-divider orientation="left">检索测试</a-divider>
        <div class="search">
          <a-input-search
            v-model:value="query"
            placeholder="输入一个问题，看会召回哪些片段"
            enter-button="检索"
            :loading="searching"
            @search="runSearch"
          />
          <span>返回</span>
          <a-input-number v-model:value="topK" :min="1" :max="20" style="width: 70px" />
          <span>条</span>
          <a-switch v-model:checked="rerank" checked-children="重排" un-checked-children="不重排" />
        </div>
        <template v-if="result">
          <div class="meta">
            {{ result.hits.length }} 条结果 · {{ formatTimings(result.timings_ms) }}
            <template v-if="rerank && !result.reranked">（服务端未启用重排）</template>
          </div>
          <a-list :data-source="result.hits" size="small">
            <template #renderItem="{ item, index }">
              <a-list-item>
                <div class="hit">
                  <div class="hit-head">
                    <b>#{{ index + 1 }}</b> {{ item.filename }}
                    <template v-if="item.page"> · 第 {{ item.page }} 页</template>
                    · 第 {{ item.seq + 1 }} 块
                    <a-tag>向量 {{ item.vector_score.toFixed(3) }}</a-tag>
                    <a-tag v-if="item.rerank_score !== null" color="blue">重排 {{ item.rerank_score.toFixed(2) }}</a-tag>
                  </div>
                  <div class="hit-text">{{ item.text }}</div>
                </div>
              </a-list-item>
            </template>
          </a-list>
        </template>
      </a-tab-pane>
      <!-- 切到这个标签页时才创建画布（cytoscape 需要可见的容器才能算尺寸） -->
      <a-tab-pane key="graph" tab="知识图谱">
        <GraphPanel v-if="tab === 'graph'" :kb-id="kbId" :readonly="readonly" />
      </a-tab-pane>
    </a-tabs>
    <a-empty v-else class="empty" description="先新建一个知识库" />

    <a-drawer :open="!!chunksOf" :title="chunksOf?.filename" width="640" @close="chunksOf = null">
      <div v-for="c in chunks" :key="c.id" class="chunk">
        <div class="chunk-head">
          第 {{ c.seq + 1 }} 块 · {{ c.token_count }} token<template v-if="c.page"> · 第 {{ c.page }} 页</template>
        </div>
        <div class="hit-text">{{ c.text }}</div>
      </div>
    </a-drawer>
  </div>
</template>

<style scoped>
.knowledge {
  padding: 24px;
  overflow-y: auto;
  height: 100%;
  box-sizing: border-box;
}
.toolbar,
.search {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.search :deep(.ant-input-search) {
  flex: 1;
  min-width: 260px;
}
.error {
  color: #cf1322;
  font-size: 12px;
}
.meta {
  color: #888;
  font-size: 12px;
  margin-bottom: 4px;
}
.hit {
  width: 100%;
}
.hit-head,
.chunk-head {
  color: #555;
  font-size: 12px;
  margin-bottom: 4px;
}
.hit-text {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.6;
}
.chunk {
  border-bottom: 1px solid #f0f0f0;
  padding: 8px 0;
}
.empty {
  margin-top: 80px;
}
</style>
