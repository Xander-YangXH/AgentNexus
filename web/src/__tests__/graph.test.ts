import { describe, expect, it } from 'vitest'
import type { GraphBuild } from '../api/types'
import { buildSummary, formatUsd, isBuildActive, mergeGraph, typeColor } from '../composables/graph'
import { describeGraphBuildError } from '../utils/errors'

const node = (id: string) => ({ id, name: id, type: '技术组件', mentions: 1 })
const edge = (id: string, source: string, target: string) => ({ id, source, target, predicate: '依赖', weight: 1 })

describe('graph helpers', () => {
  it('merges subgraphs by id and drops edges whose ends are missing', () => {
    const a = { nodes: [node('q'), node('p')], edges: [edge('e1', 'q', 'p')] }
    const b = { nodes: [{ ...node('q'), name: 'changed' }, node('r')], edges: [edge('e1', 'q', 'p'), edge('e2', 'q', 'r'), edge('e3', 'q', 'x')] }
    const m = mergeGraph(a, b)
    expect(m.nodes.map((n) => n.id)).toEqual(['q', 'p', 'r'])
    expect(m.nodes[0].name).toBe('q') // 已有的保持不变
    expect(m.edges.map((e) => e.id)).toEqual(['e1', 'e2']) // e3 的终点不在图里
  })

  it('summarizes builds and formats cost', () => {
    const b = {
      status: 'running', done_count: 3, extract_count: 10, input_tokens: 100, cached_tokens: 40, output_tokens: 20,
      cost_usd: 0, estimated_cost_usd: 0.05, entity_count: 0, relation_count: 0,
    } as GraphBuild
    expect(isBuildActive(b)).toBe(true)
    expect(buildSummary(b)).toBe('抽取中 3/10 · 输入 100（缓存 40）/ 输出 20 token')
    const done = { ...b, status: 'succeeded', entity_count: 5, relation_count: 4, cost_usd: 0.0412 } as GraphBuild
    expect(isBuildActive(done)).toBe(false)
    expect(buildSummary(done)).toContain('5 个实体、4 条关系 · 实际 $0.04（预估 $0.05）')
    expect(formatUsd(0)).toBe('$0')
    expect(formatUsd(0.0012)).toBe('$0.0012')
  })

  it('maps types to colors and build errors to copy', () => {
    expect(typeColor('技术组件')).not.toBe(typeColor('规则政策'))
    expect(typeColor('未知类型')).toBe(typeColor('概念术语'))
    expect(describeGraphBuildError('graph_store_unavailable')).toContain('不再花钱')
    expect(describeGraphBuildError('whatever')).toBe(describeGraphBuildError('internal'))
  })
})
