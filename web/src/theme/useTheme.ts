/**
 * 主题模式（decisions/0024）：跟随系统 / 浅色 / 深色。选择记在 localStorage（只是个人偏好，失败也不影响使用）；
 * 跟随系统时监听 prefers-color-scheme，系统切换时界面跟着变。
 */
import { computed, inject, provide, ref, watchEffect, type ComputedRef, type InjectionKey, type Ref } from 'vue'
import { applyCssVars, type Mode } from './tokens'

export type Preference = 'system' | 'light' | 'dark'
const KEY = 'agentnexus.theme'

export function readPreference(storage: Pick<Storage, 'getItem'> | null = safeStorage()): Preference {
  try {
    const v = storage?.getItem(KEY)
    return v === 'light' || v === 'dark' ? v : 'system'
  } catch {
    return 'system'
  }
}

export function resolveMode(pref: Preference, systemDark: boolean): Mode {
  return pref === 'system' ? (systemDark ? 'dark' : 'light') : pref
}

function safeStorage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null
  }
}

export interface ThemeContext {
  preference: Ref<Preference>
  mode: ComputedRef<Mode>
  setPreference: (p: Preference) => void
}

const THEME_KEY: InjectionKey<ThemeContext> = Symbol('theme')

export function provideTheme(): ThemeContext {
  const preference = ref<Preference>(readPreference())
  const media = typeof window !== 'undefined' && window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null
  const systemDark = ref(media?.matches ?? false)
  media?.addEventListener('change', (e) => (systemDark.value = e.matches))
  const mode = computed(() => resolveMode(preference.value, systemDark.value))
  watchEffect(() => applyCssVars(mode.value))

  function setPreference(p: Preference) {
    preference.value = p
    try {
      safeStorage()?.setItem(KEY, p)
    } catch {
      /* 隐私模式等：只是这次会话有效 */
    }
  }
  const ctx = { preference, mode, setPreference }
  provide(THEME_KEY, ctx)
  return ctx
}

export function useTheme(): ThemeContext {
  const ctx = inject(THEME_KEY)
  if (!ctx) throw new Error('useTheme() called outside provideTheme()')
  return ctx
}
