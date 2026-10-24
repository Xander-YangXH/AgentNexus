<script setup lang="ts">
// 知识库页面（decisions/0024 改版）：左侧知识库列表（我的 / 部门共享），右侧选中知识库的文档、检索测试、知识图谱。
// 数据以后端为准，这里不做本地缓存
import {
  BookOutlined,
  CloudUploadOutlined,
  DeleteOutlined,
  FileTextOutlined,
  PlusOutlined,
  ShareAltOutlined,
  TeamOutlined,
} from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api, HttpError } from '../api/http'
import type { Chunk, KnowledgeBase, KnowledgeDocument, SearchResult } from '../api/types'
import { deleteKbWarning, formatSize, formatTimings, needsPolling, STATUS_TAG } from '../composables/knowledge'
import GraphPanel from './GraphPanel.vue'
import { describeDocumentError, describeRequestError } from '../utils/errors'

const props = defineProps<{ userId: string }>()

const POLL_MS = 2000

const kbs = ref<KnowledgeBase[]>([])
const kbId = ref<string | null>(null)
const creating = ref(false)
const newKbName = ref('')
const docs = ref<KnowledgeDocument[]>([])
const uploading = ref(false)

const chunksOf = ref<KnowledgeDocument | null>(null)
const chunks = ref<Chunk[]>([])

const tab = ref<'docs' | 'search' | 'graph'>('docs')
const query = ref('')
const topK = ref(5)
const rerank = ref(true)
const searching = ref(false)
const result = ref<SearchResult | null>(null)

function showError(e: unknown) {
  if (e instanceof HttpError) message.error(describeRequestError(e.code) ?? e.describe())
  else message.error(String(e))
}

const mine = computed(() => kbs.value.filter((k) => k.user_id === props.userId))
const shared = computed(() => kbs.value.filter((k) => k.user_id !== props.userId))
// 部门共享给我的知识库（decisions/0023）：只读——能看文档、检索、浏览图谱，不能上传、重新处理、构建、改共享
const current = computed(() => kbs.value.find((k) => k.id === kbId.value) ?? null)
const readonly = computed(() => !!current.value && current.value.user_id !== props.userId)
const readyCount = computed(() => docs.value.filter((d) => d.status === 'ready').length)

async function setShared(isShared: boolean) {
  if (!current.value) return
  try {
    await api.setKnowledgeBaseVisibility(current.value.id, isShared ? 'department' : 'private')
    await loadKbs()
    message.success(isShared ? '已共享给本部门（只读）' : '已取消共享')
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
    creating.value = false
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

async function removeDoc(doc: KnowledgeDocument) {
  try {
    await api.deleteDocument(doc.id)
    if (chunksOf.value?.id === doc.id) chunksOf.value = null
    message.success(`已删除 ${doc.filename}`)
    await loadDocs()
  } catch (e) {
    showError(e)
  }
}

async function removeKb() {
  if (!current.value) return
  const name = current.value.name
  try {
    await api.deleteKnowledgeBase(current.value.id)
    message.success(`已删除知识库「${name}」`)
    await loadKbs()
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

// 固定宽度的列合计 540px：窄屏上“文件”列会被挤成 0、操作列被裁掉（阶段 7.8 用手机宽度检查时发现）。
// 表格设最小宽度 720px（:scroll.x），放不下时在卡片里横向滚动，文件名至少留 180px
const columns = [
  { title: '文件', dataIndex: 'filename', key: 'filename', ellipsis: true },
  { title: '大小', key: 'size', width: 90 },
  { title: '状态', key: 'status', width: 220 },
  { title: '分块', dataIndex: 'chunk_count', key: 'chunks', width: 70 },
  { title: '', key: 'actions', width: 160 },
]
</script>

<template>
  <div class="kb-page">
    <aside class="kb-list">
      <div class="list-head">
        <span class="list-title">知识库</span>
        <a-tooltip title="新建知识库"><a-button type="text" size="small" aria-label="新建知识库" @click="creating = !creating"><PlusOutlined /></a-button></a-tooltip>
      </div>
      <div v-if="creating" class="create">
        <a-input v-model:value="newKbName" placeholder="知识库名称" size="small" @press-enter="createKb" />
        <a-button type="primary" size="small" :disabled="!newKbName.trim()" @click="createKb">创建</a-button>
      </div>
      <div class="list-scroll">
        <template v-for="group in [{ label: '我的', items: mine }, { label: '部门共享给我', items: shared }]" :key="group.label">
          <div v-if="group.items.length" class="group-label">{{ group.label }}</div>
          <button v-for="k in group.items" :key="k.id" :class="['kb', { active: k.id === kbId }]" @click="kbId = k.id">
            <BookOutlined v-if="k.user_id === userId" class="kb-icon" />
            <TeamOutlined v-else class="kb-icon" />
            <span class="kb-name">{{ k.name }}</span>
            <ShareAltOutlined v-if="k.user_id === userId && k.visibility === 'department'" class="an-hint" title="已共享给本部门" />
          </button>
        </template>
        <div v-if="!kbs.length" class="an-hint empty-list">还没有知识库，点右上角 + 新建</div>
      </div>
    </aside>

    <section class="detail">
      <template v-if="current">
        <header class="detail-head">
          <div>
            <h1 class="an-page-title">{{ current.name }}</h1>
            <p class="an-page-subtitle">
              {{ docs.length }} 个文档 · {{ readyCount }} 个可检索
              <template v-if="readonly"> · 部门共享（只读）</template>
              <template v-else-if="current.visibility === 'department'"> · 已共享给本部门</template>
            </p>
          </div>
          <div class="head-actions">
            <a-tooltip v-if="!readonly" title="共享后，同部门的人可以检索和浏览（只读）">
              <span class="share">
                <a-switch size="small" :checked="current.visibility === 'department'" @change="(v: unknown) => setShared(Boolean(v))" />
                部门共享
              </span>
            </a-tooltip>
            <a-tag v-else color="blue"><TeamOutlined /> 只读</a-tag>
            <a-popconfirm
              v-if="!readonly"
              :title="`删除知识库「${current.name}」？`"
              :description="deleteKbWarning(current, docs.length)"
              ok-text="删除"
              cancel-text="取消"
              :ok-button-props="{ danger: true }"
              @confirm="removeKb"
            >
              <a-button danger><DeleteOutlined /> 删除</a-button>
            </a-popconfirm>
            <a-upload v-if="!readonly" :before-upload="onFile" :show-upload-list="false" accept=".txt,.md,.markdown,.pdf,.docx">
              <a-button type="primary" :loading="uploading"><CloudUploadOutlined /> 上传文档</a-button>
            </a-upload>
          </div>
        </header>

        <a-tabs v-model:active-key="tab" class="tabs">
          <a-tab-pane key="docs" tab="文档">
            <div class="an-card">
              <a-table :columns="columns" :data-source="docs" row-key="id" size="middle" :pagination="false"
                       :scroll="{ x: 720 }">
                <template #emptyText>
                  <div class="table-empty">
                    <FileTextOutlined class="table-empty-icon" />
                    <div>{{ readonly ? '这个知识库还没有文档' : '上传 .txt / .md / .pdf / .docx，后台会自动解析、切块、向量化' }}</div>
                  </div>
                </template>
                <template #bodyCell="{ column, record }">
                  <template v-if="column.key === 'filename'">
                    <span class="filename"><FileTextOutlined class="an-hint" /> {{ record.filename }}</span>
                  </template>
                  <template v-else-if="column.key === 'size'">{{ formatSize(record.size_bytes) }}</template>
                  <template v-else-if="column.key === 'status'">
                    <a-tag :color="STATUS_TAG[record.status as keyof typeof STATUS_TAG].color">
                      {{ STATUS_TAG[record.status as keyof typeof STATUS_TAG].label }}
                    </a-tag>
                    <span v-if="record.status === 'failed'" class="error">{{ describeDocumentError(record.error_code) }}</span>
                  </template>
                  <template v-else-if="column.key === 'actions'">
                    <a-button v-if="record.status === 'ready'" size="small" type="link" @click="openChunks(record)">查看分块</a-button>
                    <a-button v-if="record.status === 'failed' && !readonly" size="small" type="link" @click="reprocess(record)">重新处理</a-button>
                    <a-popconfirm
                      v-if="!readonly"
                      :title="`删除 ${record.filename}？`"
                      description="分块、向量和图谱里只出自这篇文档的内容会一起删除，无法恢复。"
                      ok-text="删除"
                      cancel-text="取消"
                      :ok-button-props="{ danger: true }"
                      @confirm="removeDoc(record as KnowledgeDocument)"
                    >
                      <a-button size="small" type="link" danger>删除</a-button>
                    </a-popconfirm>
                  </template>
                </template>
              </a-table>
            </div>
          </a-tab-pane>

          <a-tab-pane key="search" tab="检索测试">
            <div class="an-card an-card-body">
              <p class="an-muted search-help">输入一个问题，看会召回哪些片段、各自的分数和每一步的耗时——用来判断“问不出来”是检索的问题还是模型的问题。</p>
              <div class="search">
                <a-input-search v-model:value="query" placeholder="例如：退货要几天？" enter-button="检索" :loading="searching" @search="runSearch" />
                <span class="an-muted">返回</span>
                <a-input-number v-model:value="topK" :min="1" :max="20" style="width: 70px" />
                <a-switch v-model:checked="rerank" checked-children="重排" un-checked-children="不重排" />
              </div>
            </div>
            <template v-if="result">
              <div class="meta an-hint">
                {{ result.hits.length }} 条结果 · {{ formatTimings(result.timings_ms) }}
                <template v-if="rerank && !result.reranked">（服务端未启用重排）</template>
              </div>
              <div v-for="(item, index) in result.hits" :key="item.chunk_id" class="hit an-card">
                <div class="hit-head">
                  <span class="hit-rank">{{ index + 1 }}</span>
                  <span class="hit-file">{{ item.filename }}<template v-if="item.page"> · 第 {{ item.page }} 页</template> · 第 {{ item.seq + 1 }} 块</span>
                  <a-tag title="召回分数：向量与关键词按排名融合后的分数，越大越靠前">召回 {{ item.vector_score.toFixed(3) }}</a-tag>
                  <a-tag v-if="item.rerank_score !== null" color="blue">重排 {{ item.rerank_score.toFixed(2) }}</a-tag>
                </div>
                <div class="hit-text">{{ item.text }}</div>
              </div>
            </template>
          </a-tab-pane>

          <!-- 切到这个标签页时才创建画布（cytoscape 需要可见的容器才能算尺寸） -->
          <a-tab-pane key="graph" tab="知识图谱">
            <GraphPanel v-if="tab === 'graph'" :kb-id="current.id" :readonly="readonly" />
          </a-tab-pane>
        </a-tabs>
      </template>
      <div v-else class="detail-empty">
        <BookOutlined class="detail-empty-icon" />
        <h2>还没有知识库</h2>
        <p class="an-muted">新建一个知识库并上传文档，就可以在对话里关联它，回答会附上引用。</p>
        <a-button type="primary" @click="creating = true"><PlusOutlined /> 新建知识库</a-button>
      </div>
    </section>

    <a-drawer :open="!!chunksOf" :title="chunksOf?.filename" width="640" @close="chunksOf = null">
      <div v-for="c in chunks" :key="c.id" class="chunk">
        <div class="chunk-head an-hint">
          第 {{ c.seq + 1 }} 块 · {{ c.token_count }} token<template v-if="c.page"> · 第 {{ c.page }} 页</template>
        </div>
        <div class="hit-text">{{ c.text }}</div>
      </div>
    </a-drawer>
  </div>
</template>

<style scoped>
.kb-page {
  display: flex;
  height: 100%;
}
.kb-list {
  width: 260px;
  flex: none;
  display: flex;
  flex-direction: column;
  background: var(--an-surface);
  border-right: 1px solid var(--an-border);
}
.list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px 12px 10px 16px;
}
.list-title {
  font-size: 15px;
  font-weight: 600;
}
.create {
  display: flex;
  gap: 6px;
  padding: 0 12px 10px;
}
.list-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 0 8px 12px;
}
.group-label {
  padding: 10px 8px 4px;
  font-size: 12px;
  color: var(--an-text-3);
}
.kb {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--an-text);
  text-align: left;
  cursor: pointer;
}
.kb:hover {
  background: var(--an-surface-2);
}
.kb.active {
  background: var(--an-primary-soft);
  color: var(--an-primary-text);
}
.kb-icon {
  color: var(--an-text-3);
}
.kb.active .kb-icon {
  color: var(--an-primary);
}
.kb-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.empty-list {
  padding: 16px 8px;
}
.detail {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  padding: 28px 32px 40px;
}
.detail-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.head-actions {
  display: flex;
  flex-wrap: wrap; /* 加了“删除”按钮后窄屏放不下：整体换行，而不是把“部门共享”挤成两行 */
  align-items: center;
  gap: 12px 16px;
}
.share {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--an-text-2);
  font-size: 13px;
  white-space: nowrap;
}
.tabs :deep(.ant-tabs-nav) {
  margin-bottom: 16px;
}
.filename {
  display: inline-flex;
  gap: 8px;
  align-items: center;
}
.error {
  color: var(--an-danger);
  font-size: 12px;
}
.table-empty {
  padding: 28px 0;
  color: var(--an-text-3);
}
.table-empty-icon {
  font-size: 28px;
  margin-bottom: 8px;
}
.search-help {
  margin: 0 0 12px;
  font-size: 13px;
}
.search {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}
.search :deep(.ant-input-search) {
  flex: 1;
  min-width: 260px;
}
.meta {
  margin: 16px 0 8px;
}
.hit {
  padding: 12px 16px;
  margin-bottom: 10px;
}
.hit-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 6px;
  font-size: 12px;
}
.hit-rank {
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
  border-radius: 5px;
  font-weight: 600;
  background: var(--an-primary-soft);
  color: var(--an-primary-text);
}
.hit-file {
  color: var(--an-text-2);
  flex: 1;
  min-width: 0;
}
.hit-text {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.7;
}
.chunk {
  border-bottom: 1px solid var(--an-border);
  padding: 10px 0;
}
.chunk-head {
  margin-bottom: 4px;
}
.detail-empty {
  max-width: 420px;
  margin: 120px auto 0;
  text-align: center;
}
.detail-empty-icon {
  font-size: 36px;
  color: var(--an-primary);
}
.detail-empty h2 {
  font-size: 20px;
  font-weight: 600;
  margin: 12px 0 6px;
}
@media (max-width: 860px) {
  .kb-page {
    flex-direction: column;
  }
  .kb-list {
    width: 100%;
    max-height: 200px;
    border-right: 0;
    border-bottom: 1px solid var(--an-border);
  }
  .detail {
    padding: 20px 16px;
  }
}
</style>
