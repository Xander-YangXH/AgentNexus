import { describe, expect, it } from 'vitest'
import type { Message } from '../api/types'
import { mergeMessages } from '../composables/messagePages'

const m = (seq: number, content = `m${seq}`): Message => ({ id: `id${seq}`, seq, role: 'user', content, created_at: '' })

describe('mergeMessages', () => {
  it('keeps older pages when the latest page is reloaded', () => {
    const loaded = [m(1), m(2), m(3), m(4)] // 用户已经翻出了更早的 1、2
    const latestPage = [m(3), m(4), m(5)] // 刷新只拉到最近一页
    expect(mergeMessages(loaded, latestPage).map((x) => x.seq)).toEqual([1, 2, 3, 4, 5])
  })

  it('prepends an older page and dedupes overlap', () => {
    expect(mergeMessages([m(5), m(6)], [m(3), m(4), m(5)]).map((x) => x.seq)).toEqual([3, 4, 5, 6])
  })

  it('prefers the incoming copy of the same message', () => {
    expect(mergeMessages([m(1, 'old')], [m(1, 'new')])[0].content).toBe('new')
  })
})
