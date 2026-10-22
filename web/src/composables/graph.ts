import type { GraphBuild, GraphData } from '../api/types'

/** 把新取到的子图（展开邻居、最短路径）并进当前画布的数据：按 id 去重，已有的保持不变 */
export function mergeGraph(current: GraphData, incoming: GraphData): GraphData {
  const nodes = new Map(current.nodes.map((n) => [n.id, n]))
  for (const n of incoming.nodes) if (!nodes.has(n.id)) nodes.set(n.id, n)
  const edges = new Map(current.edges.map((e) => [e.id, e]))
  for (const e of incoming.edges) if (!edges.has(e.id) && nodes.has(e.source) && nodes.has(e.target)) edges.set(e.id, e)
  return { nodes: [...nodes.values()], edges: [...edges.values()] }
}

export function isBuildActive(b: GraphBuild | null | undefined): boolean {
  return b?.status === 'pending' || b?.status === 'running'
}

export function formatUsd(v: number): string {
  if (v === 0) return '$0'
  return v < 0.01 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`
}

/** 构建进度 / 结果的一行说明 */
export function buildSummary(b: GraphBuild): string {
  const tokens = `输入 ${b.input_tokens}（缓存 ${b.cached_tokens}）/ 输出 ${b.output_tokens} token`
  if (b.status === 'pending') return '排队中'
  if (b.status === 'running') return `抽取中 ${b.done_count}/${b.extract_count} · ${tokens}`
  const cost = `实际 ${formatUsd(b.cost_usd)}（预估 ${formatUsd(b.estimated_cost_usd)}）`
  if (b.status === 'succeeded') return `${b.entity_count} 个实体、${b.relation_count} 条关系 · ${cost} · ${tokens}`
  return `已抽取 ${b.done_count}/${b.extract_count} · ${cost}`
}

// 实体类型 → 颜色（与后端 knowledge/graph/extraction.py 的 ENTITY_TYPES 对应）
const TYPE_COLORS: Record<string, string> = {
  产品服务: '#1677ff',
  规则政策: '#fa8c16',
  流程操作: '#52c41a',
  组织角色: '#eb2f96',
  渠道地点: '#13c2c2',
  技术组件: '#722ed1',
  概念术语: '#8c8c8c',
}

export function typeColor(type: string): string {
  return TYPE_COLORS[type] ?? '#8c8c8c'
}

export const ENTITY_TYPES = Object.keys(TYPE_COLORS)
