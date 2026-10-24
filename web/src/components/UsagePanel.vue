<script setup lang="ts">
// 用量看板（decisions/0023、0024 改版）：按天的请求、模型调用、token、检索与估算费用。
// 范围按角色：成员只看自己；部门负责人可看本部门；管理员可看全站或指定部门（后端同样检查，越权 403）
import { ApiOutlined, DollarOutlined, MessageOutlined, SearchOutlined, VerticalAlignBottomOutlined, VerticalAlignTopOutlined } from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import { BarChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { init, use, type ECharts } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, HttpError } from '../api/http'
import type { Department, Me, UsageScope, UsageSummary } from '../api/types'
import { fillDays, formatTokens, scopesFor } from '../composables/usage'
import { PALETTES } from '../theme/tokens'
import { useTheme } from '../theme/useTheme'

// 按需引入：只打包用到的图表和组件（整包 ECharts 压缩后约 1MB）
use([BarChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

const props = defineProps<{ me: Me }>()
const { mode } = useTheme()
const scopes = computed(() => scopesFor(props.me))
const scope = ref<UsageScope>('me')
const days = ref(30)
const departmentId = ref<string | null>(null)
const departments = ref<Department[]>([])
const data = ref<UsageSummary | null>(null)
const tokensEl = ref<HTMLElement | null>(null)
const countsEl = ref<HTMLElement | null>(null)
let charts: ECharts[] = []

const metrics = computed(() => {
  const t = data.value?.totals
  if (!t) return []
  return [
    { label: '请求', value: String(t.runs), icon: MessageOutlined },
    { label: '模型调用', value: String(t.llm_calls), icon: ApiOutlined },
    { label: '检索', value: String(t.searches), icon: SearchOutlined },
    { label: '输入 token', value: formatTokens(t.input_tokens), icon: VerticalAlignTopOutlined,
      hint: t.input_tokens ? `缓存命中 ${Math.round((t.cached_tokens / t.input_tokens) * 100)}%` : '' },
    { label: '输出 token', value: formatTokens(t.output_tokens), icon: VerticalAlignBottomOutlined },
    { label: '估算费用', value: `$${t.cost_usd.toFixed(4)}`, icon: DollarOutlined, hint: '按配置的单价估算' },
  ]
})

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
  const p = PALETTES[mode.value]
  const rows = fillDays(data.value.series, days.value)
  const x = rows.map((r) => r.day.slice(5))
  // 坐标轴、提示框用当前主题的颜色；用柱状图：数据稀疏时（多数天为 0）折线会画成尖峰，柱状更直观
  const base = {
    color: p.chart,
    textStyle: { color: p.text2, fontFamily: 'inherit' },
    // ECharts 6 的图例默认放在底部（5 之前在顶部），会压住横轴标签：显式放到顶部
    legend: { top: 0, right: 0, icon: 'roundRect', itemWidth: 10, itemHeight: 10, textStyle: { color: p.text2 } },
    grid: { left: 8, right: 8, top: 36, bottom: 4, containLabel: true },
    tooltip: { trigger: 'axis', backgroundColor: p.surface, borderColor: p.border, textStyle: { color: p.text } },
    xAxis: { type: 'category', data: x, axisLine: { lineStyle: { color: p.border } }, axisTick: { show: false },
             axisLabel: { color: p.text3 } },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: p.border, type: 'dashed' } }, axisLabel: { color: p.text3 } },
  }
  charts.forEach((c) => c.dispose())
  const tokens = init(tokensEl.value)
  tokens.setOption({
    ...base,
    yAxis: { ...base.yAxis, axisLabel: { color: p.text3, formatter: formatTokens } },
    series: [
      { name: '输入（未命中缓存）', type: 'bar', stack: 't', barMaxWidth: 18, data: rows.map((r) => r.input_tokens - r.cached_tokens) },
      { name: '输入（缓存命中）', type: 'bar', stack: 't', barMaxWidth: 18, data: rows.map((r) => r.cached_tokens) },
      { name: '输出', type: 'bar', stack: 't', barMaxWidth: 18, itemStyle: { borderRadius: [3, 3, 0, 0] },
        data: rows.map((r) => r.output_tokens) },
    ],
  })
  const counts = init(countsEl.value)
  counts.setOption({
    ...base,
    color: [p.chart[0], p.chart[2], p.chart[3]],
    yAxis: { ...base.yAxis, minInterval: 1 },
    series: [
      { name: '请求', type: 'bar', barMaxWidth: 10, itemStyle: { borderRadius: [3, 3, 0, 0] }, data: rows.map((r) => r.runs) },
      { name: '模型调用', type: 'bar', barMaxWidth: 10, itemStyle: { borderRadius: [3, 3, 0, 0] }, data: rows.map((r) => r.llm_calls) },
      { name: '检索', type: 'bar', barMaxWidth: 10, itemStyle: { borderRadius: [3, 3, 0, 0] }, data: rows.map((r) => r.searches) },
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
watch(mode, draw) // 切换浅色 / 深色：用新颜色重画

const userColumns = [
  { title: '用户', dataIndex: 'name', key: 'name' },
  { title: '请求', dataIndex: 'runs', key: 'runs', width: 80, align: 'right' as const },
  { title: '模型调用', dataIndex: 'llm_calls', key: 'llm_calls', width: 100, align: 'right' as const },
  { title: '检索', dataIndex: 'searches', key: 'searches', width: 80, align: 'right' as const },
  { title: 'Token（入 / 出）', key: 'tokens', width: 160, align: 'right' as const },
  { title: '估算费用', key: 'cost', width: 110, align: 'right' as const },
]
</script>

<template>
  <div class="an-page">
    <header class="an-page-header">
      <div>
        <h1 class="an-page-title">用量</h1>
        <p class="an-page-subtitle">请求、模型调用、token 与检索次数，按天统计</p>
      </div>
      <div class="controls">
        <a-segmented v-model:value="scope" :options="scopes" />
        <a-select
          v-if="scope === 'department' && me.role === 'admin'"
          v-model:value="departmentId"
          placeholder="我的部门"
          allow-clear
          style="width: 160px"
          :options="departments.map((d) => ({ value: d.id, label: d.name }))"
        />
        <a-select v-model:value="days" style="width: 120px" :options="[7, 30, 90].map((d) => ({ value: d, label: `最近 ${d} 天` }))" />
      </div>
    </header>

    <template v-if="data">
      <div class="metrics">
        <div v-for="m in metrics" :key="m.label" class="metric an-card">
          <div class="metric-head"><component :is="m.icon" /> {{ m.label }}</div>
          <div class="metric-value">{{ m.value }}</div>
          <div class="an-hint">{{ m.hint || ' ' }}</div>
        </div>
      </div>
      <div class="charts">
        <div class="an-card">
          <div class="an-card-header">每天的 token</div>
          <div ref="tokensEl" class="chart" />
        </div>
        <div class="an-card">
          <div class="an-card-header">每天的请求、模型调用与检索</div>
          <div ref="countsEl" class="chart" />
        </div>
      </div>
      <div v-if="data.by_user.length" class="an-card users">
        <div class="an-card-header">按用户（前 20）</div>
        <a-table :columns="userColumns" :data-source="data.by_user" row-key="user_id" size="middle" :pagination="false">
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'tokens'">{{ formatTokens(record.input_tokens) }} / {{ formatTokens(record.output_tokens) }}</template>
            <template v-else-if="column.key === 'cost'">${{ record.cost_usd.toFixed(4) }}</template>
          </template>
        </a-table>
      </div>
    </template>
    <a-skeleton v-else active />
  </div>
</template>

<style scoped>
.controls {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.metrics {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.metric {
  padding: 14px 16px;
}
.metric-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--an-text-2);
}
.metric-value {
  margin: 6px 0 2px;
  font-size: 24px;
  font-weight: 600;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
}
.charts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}
.chart {
  height: 280px;
  margin: 12px 14px 14px;
}
.users :deep(.ant-table) {
  background: transparent;
}
@media (max-width: 1100px) {
  .charts {
    grid-template-columns: 1fr;
  }
}
</style>
