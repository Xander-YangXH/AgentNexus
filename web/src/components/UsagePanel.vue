<script setup lang="ts">
// 用量看板（decisions/0023）：按天的请求数、模型调用、token、检索次数与估算费用。
// 范围按角色：成员只看自己；部门负责人可看本部门；管理员可看全站或指定部门（后端同样检查，越权 403）
import { message } from 'ant-design-vue'
import { BarChart, LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { init, use, type ECharts } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, HttpError } from '../api/http'
import type { Department, Me, UsageScope, UsageSummary } from '../api/types'
import { fillDays, formatTokens, scopesFor } from '../composables/usage'

// 按需引入：只打包用到的图表和组件（整包 ECharts 压缩后约 1MB）
use([LineChart, BarChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

const props = defineProps<{ me: Me }>()
const scopes = computed(() => scopesFor(props.me))
const scope = ref<UsageScope>('me')
const days = ref(30)
const departmentId = ref<string | null>(null)
const departments = ref<Department[]>([])
const data = ref<UsageSummary | null>(null)
const tokensEl = ref<HTMLElement | null>(null)
const countsEl = ref<HTMLElement | null>(null)
let charts: ECharts[] = []

async function load() {
  try {
    data.value = await api.usageSummary(scope.value, days.value, scope.value === 'department' ? departmentId.value : null)
    await nextTick()
    draw()
  } catch (e) {
    message.error(e instanceof HttpError ? e.describe() : String(e))
  }
}

function draw() {
  if (!data.value || !tokensEl.value || !countsEl.value) return
  const rows = fillDays(data.value.series, days.value)
  const x = rows.map((r) => r.day.slice(5))
  charts.forEach((c) => c.dispose())
  const tokens = init(tokensEl.value)
  tokens.setOption({
    tooltip: { trigger: 'axis' },
    // ECharts 6 的图例默认放在底部（5 之前在顶部），会压住横轴标签：显式放到顶部
    legend: { top: 0, data: ['输入（未命中缓存）', '输入（缓存命中）', '输出'] },
    grid: { left: 60, right: 20, top: 40, bottom: 30 },
    xAxis: { type: 'category', data: x },
    yAxis: { type: 'value', axisLabel: { formatter: formatTokens } },
    series: [
      { name: '输入（未命中缓存）', type: 'line', stack: 'in', areaStyle: {}, data: rows.map((r) => r.input_tokens - r.cached_tokens) },
      { name: '输入（缓存命中）', type: 'line', stack: 'in', areaStyle: {}, data: rows.map((r) => r.cached_tokens) },
      { name: '输出', type: 'line', data: rows.map((r) => r.output_tokens) },
    ],
  })
  const counts = init(countsEl.value)
  counts.setOption({
    tooltip: { trigger: 'axis' },
    legend: { top: 0, data: ['请求', '模型调用', '检索'] },
    grid: { left: 60, right: 20, top: 40, bottom: 30 },
    xAxis: { type: 'category', data: x },
    yAxis: { type: 'value', minInterval: 1 },
    series: [
      { name: '请求', type: 'bar', data: rows.map((r) => r.runs) },
      { name: '模型调用', type: 'bar', data: rows.map((r) => r.llm_calls) },
      { name: '检索', type: 'bar', data: rows.map((r) => r.searches) },
    ],
  })
  charts = [tokens, counts]
}

const resize = () => charts.forEach((c) => c.resize())
onMounted(async () => {
  window.addEventListener('resize', resize)
  if (props.me.role === 'admin') departments.value = await api.listDepartments().catch(() => [])
  await load()
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  charts.forEach((c) => c.dispose())
})
watch([scope, days, departmentId], load)

const userColumns = [
  { title: '用户', dataIndex: 'name', key: 'name' },
  { title: '请求', dataIndex: 'runs', key: 'runs', width: 80 },
  { title: '模型调用', dataIndex: 'llm_calls', key: 'llm_calls', width: 100 },
  { title: '检索', dataIndex: 'searches', key: 'searches', width: 80 },
  { title: 'Token（入 / 出）', key: 'tokens', width: 160 },
  { title: '估算费用', key: 'cost', width: 110 },
]
</script>

<template>
  <div class="usage">
    <div class="toolbar">
      <a-segmented v-model:value="scope" :options="scopes" />
      <a-select
        v-if="scope === 'department' && me.role === 'admin'"
        v-model:value="departmentId"
        placeholder="我的部门"
        allow-clear
        style="width: 180px"
        :options="departments.map((d) => ({ value: d.id, label: d.name }))"
      />
      <a-select v-model:value="days" style="width: 110px" :options="[7, 30, 90].map((d) => ({ value: d, label: `最近 ${d} 天` }))" />
    </div>
    <template v-if="data">
      <div class="stats">
        <a-statistic title="请求" :value="data.totals.runs" />
        <a-statistic title="模型调用" :value="data.totals.llm_calls" />
        <a-statistic title="检索" :value="data.totals.searches" />
        <a-statistic title="输入 token" :value="formatTokens(data.totals.input_tokens)" />
        <a-statistic title="输出 token" :value="formatTokens(data.totals.output_tokens)" />
        <a-statistic title="估算费用（美元）" :value="data.totals.cost_usd.toFixed(4)" />
      </div>
      <div class="chart-title">每天的 token</div>
      <div ref="tokensEl" class="chart" />
      <div class="chart-title">每天的请求、模型调用与检索</div>
      <div ref="countsEl" class="chart" />
      <template v-if="data.by_user.length">
        <div class="chart-title">按用户（前 20）</div>
        <a-table :columns="userColumns" :data-source="data.by_user" row-key="user_id" size="small" :pagination="false">
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'tokens'">{{ formatTokens(record.input_tokens) }} / {{ formatTokens(record.output_tokens) }}</template>
            <template v-else-if="column.key === 'cost'">${{ record.cost_usd.toFixed(4) }}</template>
          </template>
        </a-table>
      </template>
    </template>
  </div>
</template>

<style scoped>
.usage {
  padding: 20px 24px;
  overflow-y: auto;
  height: 100%;
  box-sizing: border-box;
}
.toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}
.stats {
  display: flex;
  gap: 32px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.chart-title {
  font-weight: 600;
  margin: 12px 0 4px;
}
.chart {
  height: 260px;
}
</style>
