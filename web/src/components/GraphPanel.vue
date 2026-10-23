<script setup lang="ts">
// 知识图谱（decisions/0019）：构建（先看预估费用）→ 浏览（概览、搜索、展开邻居、最短路径）→ 溯源（出处原文）
import cytoscape, { type Core } from 'cytoscape'
import { message } from 'ant-design-vue'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, HttpError } from '../api/http'
import type { EntityDetail, GraphBuild, GraphData, GraphEstimate, GraphNode } from '../api/types'
import { buildSummary, ENTITY_TYPES, formatUsd, isBuildActive, mergeGraph, typeColor } from '../composables/graph'
import { describeGraphBuildError, describeRequestError } from '../utils/errors'

// readonly：部门共享给我的知识库（decisions/0023），只能浏览，不能预估和构建（会花所有者的钱）
const props = defineProps<{ kbId: string; readonly?: boolean }>()

const estimate = ref<GraphEstimate | null>(null)
const build = ref<GraphBuild | null>(null) // 最近一次构建
const starting = ref(false)
const graph = ref<GraphData>({ nodes: [], edges: [] })
const selected = ref<EntityDetail | null>(null)
const searchText = ref('')
const searchResults = ref<GraphNode[]>([])
const pathFrom = ref<{ id: string; name: string } | null>(null)
const container = ref<HTMLElement | null>(null)
let cy: Core | null = null
let timer: ReturnType<typeof setTimeout> | null = null

const hasGraph = computed(() => graph.value.nodes.length > 0)

function showError(e: unknown) {
  if (e instanceof HttpError) message.error(describeRequestError(e.code) ?? e.describe())
  else message.error(String(e))
}

// ---------- 构建 ----------

async function loadBuildState() {
  const id = props.kbId
  const [est, builds] = await Promise.all([props.readonly ? null : api.graphEstimate(id), api.listGraphBuilds(id)])
  if (id !== props.kbId) return
  estimate.value = est
  const wasActive = isBuildActive(build.value)
  build.value = builds[0] ?? null
  if (timer) clearTimeout(timer)
  if (isBuildActive(build.value)) timer = setTimeout(() => loadBuildState().catch(showError), 2000)
  else if (wasActive && build.value?.status === 'succeeded') await loadOverview() // 刚构建完：刷新画布
}

async function startBuild() {
  starting.value = true
  try {
    build.value = await api.createGraphBuild(props.kbId)
    await loadBuildState()
  } catch (e) {
    showError(e)
  } finally {
    starting.value = false
  }
}

// ---------- 画布 ----------

function elements(data: GraphData) {
  const maxMentions = Math.max(1, ...data.nodes.map((n) => n.mentions))
  return [
    ...data.nodes.map((n) => ({
      group: 'nodes' as const,
      data: { id: n.id, label: n.name, color: typeColor(n.type), size: 18 + 30 * Math.sqrt(n.mentions / maxMentions) },
    })),
    ...data.edges.map((e) => ({ group: 'edges' as const, data: { id: e.id, source: e.source, target: e.target, label: e.predicate } })),
  ]
}

function render(data: GraphData, { fit = true } = {}) {
  if (!cy) return
  const existing = new Set(cy.elements().map((el) => el.id()))
  const added = cy.add(elements(data).filter((el) => !existing.has(el.data.id)))
  // 新增的少量节点只在附近布局；整体重画时用力导向布局
  const layout = fit ? cy.layout({ name: 'cose', animate: false, nodeRepulsion: () => 9000, idealEdgeLength: () => 90 })
    : added.union(added.neighborhood()).layout({ name: 'cose', animate: false, fit: false })
  layout.run()
  if (fit) cy.fit(undefined, 30)
}

async function loadOverview() {
  const id = props.kbId
  const data = await api.graphOverview(id)
  if (id !== props.kbId) return
  graph.value = data
  selected.value = null
  cy?.elements().remove()
  render(data)
}

async function select(id: string) {
  highlight(id)
  try {
    selected.value = await api.entityDetail(id)
  } catch (e) {
    showError(e)
  }
}

function highlight(id: string) {
  if (!cy) return
  cy.elements().removeClass('dim focus path')
  const node = cy.getElementById(id)
  if (node.empty()) return
  cy.elements().not(node.closedNeighborhood()).addClass('dim')
  node.addClass('focus')
  cy.animate({ center: { eles: node } }, { duration: 200 })
}

async function expand(id: string) {
  try {
    const data = await api.entityNeighbors(id)
    graph.value = mergeGraph(graph.value, data)
    render(data, { fit: false })
    highlight(id)
  } catch (e) {
    showError(e)
  }
}

async function runSearch() {
  const q = searchText.value.trim()
  if (!q) return
  try {
    searchResults.value = await api.graphSearch(props.kbId, q)
    if (!searchResults.value.length) message.info('没有找到匹配的实体')
  } catch (e) {
    showError(e)
  }
}

async function focusResult(n: GraphNode) {
  if (!cy?.getElementById(n.id).nonempty()) {
    graph.value = mergeGraph(graph.value, { nodes: [n], edges: [] })
    await expand(n.id) // 不在画布上：连同邻居一起加进来
  }
  await select(n.id)
}

async function findPath(target: { id: string; name: string }) {
  if (!pathFrom.value) return
  try {
    const data = await api.graphPath(props.kbId, pathFrom.value.id, target.id)
    if (!data) {
      message.info(`“${pathFrom.value.name}”与“${target.name}”在 4 跳内没有关联`)
      return
    }
    graph.value = mergeGraph(graph.value, data)
    render(data, { fit: false })
    cy?.elements().removeClass('dim focus path').addClass('dim')
    for (const n of data.nodes) cy?.getElementById(n.id).removeClass('dim').addClass('path')
    for (const e of data.edges) cy?.getElementById(e.id).removeClass('dim').addClass('path')
    message.success(`最短关联：${data.nodes.map((n) => n.name).join(' → ')}`)
  } catch (e) {
    showError(e)
  } finally {
    pathFrom.value = null
  }
}

onMounted(() => {
  cy = cytoscape({
    container: container.value,
    minZoom: 0.2,
    maxZoom: 3,
    style: [
      {
        selector: 'node',
        style: {
          'background-color': 'data(color)', label: 'data(label)', width: 'data(size)', height: 'data(size)',
          'font-size': 11, 'text-valign': 'bottom', 'text-margin-y': 4, color: '#333',
        },
      },
      {
        selector: 'edge',
        style: {
          width: 1.2, 'line-color': '#c9c9c9', 'target-arrow-color': '#c9c9c9', 'target-arrow-shape': 'triangle',
          'curve-style': 'bezier', label: 'data(label)', 'font-size': 9, color: '#888', 'text-rotation': 'autorotate',
        },
      },
      { selector: '.dim', style: { opacity: 0.15 } },
      { selector: 'node.focus', style: { 'border-width': 3, 'border-color': '#000' } },
      { selector: '.path', style: { 'line-color': '#f5222d', 'target-arrow-color': '#f5222d', 'border-width': 3, 'border-color': '#f5222d', width: 3 } },
    ],
  })
  cy.on('tap', 'node', (evt) => {
    const id = evt.target.id()
    if (pathFrom.value) findPath({ id, name: evt.target.data('label') })
    else select(id)
  })
  cy.on('tap', (evt) => {
    if (evt.target === cy) {
      cy?.elements().removeClass('dim focus path')
      selected.value = null
    }
  })
})

// 详情栏出现 / 消失会改变画布宽度：cytoscape 不会自己察觉，要通知它重新计算尺寸
watch(
  () => !!selected.value,
  async () => {
    await nextTick()
    cy?.resize()
  },
)

watch(
  () => props.kbId,
  () => {
    graph.value = { nodes: [], edges: [] }
    selected.value = null
    searchResults.value = []
    cy?.elements().remove()
    loadBuildState().then(() => (build.value?.status === 'succeeded' ? loadOverview() : undefined)).catch(showError)
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  if (timer) clearTimeout(timer)
  cy?.destroy()
})
</script>

<template>
  <div class="graph">
    <div class="build-bar">
      <template v-if="estimate">
        <span class="estimate">
          {{ estimate.chunk_count }} 个分块，去重后 {{ estimate.unique_count }} 个，已有抽取结果 {{ estimate.cached_count }} 个，
          需调用模型 <b>{{ estimate.extract_count }}</b> 次，预估 <b>{{ formatUsd(estimate.estimated_cost_usd) }}</b>
        </span>
        <a-popconfirm
          :title="`将调用模型 ${estimate.extract_count} 次，预估费用 ${formatUsd(estimate.estimated_cost_usd)}，确定构建？`"
          ok-text="构建"
          cancel-text="取消"
          :disabled="isBuildActive(build) || estimate.chunk_count === 0"
          @confirm="startBuild"
        >
          <a-button type="primary" :loading="starting || isBuildActive(build)" :disabled="estimate.chunk_count === 0">
            {{ build?.status === 'succeeded' ? '重新构建' : '构建图谱' }}
          </a-button>
        </a-popconfirm>
      </template>
    </div>
    <div v-if="build" class="build-status">
      <a-tag :color="{ pending: 'default', running: 'processing', succeeded: 'success', failed: 'error' }[build.status]">
        {{ { pending: '排队中', running: '构建中', succeeded: '已完成', failed: '失败' }[build.status] }}
      </a-tag>
      {{ buildSummary(build) }}
      <span v-if="build.status === 'failed'" class="error">{{ describeGraphBuildError(build.error_code) }}</span>
    </div>

    <div class="toolbar">
      <a-input-search v-model:value="searchText" placeholder="搜索实体" enter-button style="width: 260px" @search="runSearch" />
      <a-button :disabled="!hasGraph" @click="loadOverview">回到概览</a-button>
      <span class="legend">
        <span v-for="t in ENTITY_TYPES" :key="t"><i :style="{ background: typeColor(t) }" />{{ t }}</span>
      </span>
    </div>
    <div v-if="searchResults.length" class="results">
      <a-tag v-for="n in searchResults" :key="n.id" class="result" @click="focusResult(n)">{{ n.name }}（{{ n.mentions }}）</a-tag>
    </div>
    <a-alert v-if="pathFrom" type="info" show-icon class="hint" :message="`点击另一个节点，查找与“${pathFrom.name}”的最短关联`">
      <template #action><a-button size="small" @click="pathFrom = null">取消</a-button></template>
    </a-alert>

    <div class="main">
      <div ref="container" class="canvas">
        <a-empty v-if="!hasGraph" class="empty" :description="build?.status === 'succeeded' ? '图谱是空的' : '还没有构建图谱'" />
      </div>
      <div v-if="selected" class="side">
        <h3>{{ selected.name }} <a-tag :color="typeColor(selected.type)">{{ selected.type }}</a-tag></h3>
        <p class="desc">{{ selected.description }}</p>
        <p class="meta">出现在 {{ selected.mention_count }} 个分块中</p>
        <div class="actions">
          <a-button size="small" @click="expand(selected.id)">展开邻居</a-button>
          <a-button size="small" @click="pathFrom = { id: selected.id, name: selected.name }">找关联路径</a-button>
        </div>
        <h4>关系（{{ selected.relations.length }}）</h4>
        <div v-for="r in selected.relations" :key="r.id + r.direction" class="rel">
          <template v-if="r.direction === 'out'">→ <b>{{ r.predicate }}</b> <a @click="focusResult({ id: r.other_id, name: r.other_name, type: '', mentions: 0 })">{{ r.other_name }}</a></template>
          <template v-else><a @click="focusResult({ id: r.other_id, name: r.other_name, type: '', mentions: 0 })">{{ r.other_name }}</a> <b>{{ r.predicate }}</b> →</template>
          <div class="rel-desc">{{ r.description }}</div>
        </div>
        <h4>出处（{{ selected.mentions.length }}）</h4>
        <div v-for="m in selected.mentions" :key="m.chunk_id" class="mention">
          <div class="meta">{{ m.filename }}<template v-if="m.page"> · 第 {{ m.page }} 页</template> · 第 {{ m.seq + 1 }} 块</div>
          <div class="mention-text">{{ m.text }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.build-bar,
.toolbar,
.build-status {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.estimate,
.build-status {
  font-size: 12px;
  color: #555;
}
.error {
  color: #cf1322;
}
.legend {
  display: flex;
  gap: 10px;
  font-size: 12px;
  color: #666;
  flex-wrap: wrap;
}
.legend i {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  margin-right: 4px;
}
.results {
  margin-bottom: 8px;
}
.result {
  cursor: pointer;
}
.hint {
  margin-bottom: 8px;
}
.main {
  display: flex;
  gap: 12px;
}
.canvas {
  position: relative;
  flex: 1;
  min-width: 0; /* 允许收缩：cytoscape 给内部 canvas 设了固定宽度，不加这行详情栏会被挤成 0 宽（实测踩到） */
  height: 560px;
  border: 1px solid #f0f0f0;
  border-radius: 8px;
}
.empty {
  position: absolute;
  top: 40%;
  left: 50%;
  transform: translate(-50%, -50%);
}
.side {
  flex: none;
  width: 340px;
  max-height: 560px;
  overflow-y: auto;
  font-size: 13px;
}
.desc {
  color: #333;
}
.meta {
  color: #888;
  font-size: 12px;
}
.actions {
  display: flex;
  gap: 6px;
  margin-bottom: 8px;
}
.rel {
  margin-bottom: 6px;
}
.rel-desc {
  color: #888;
  font-size: 12px;
}
.mention {
  border-top: 1px solid #f0f0f0;
  padding: 6px 0;
}
.mention-text {
  white-space: pre-wrap;
  font-size: 12px;
  line-height: 1.6;
  max-height: 160px;
  overflow-y: auto;
}
</style>
