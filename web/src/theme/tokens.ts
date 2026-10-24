/**
 * 设计 token（decisions/0024）：颜色、字号、圆角集中在这里。
 * 同一份定义喂给两处：Ant Design Vue 的主题（组件库内部样式），以及 :root 上的 CSS 变量（我们自己写的样式）。
 * 业务组件里不写颜色值，只用 var(--an-*)：换配色、加暗色都只改这一个文件。
 */
import { theme as antdThemes } from 'ant-design-vue'

export type Mode = 'light' | 'dark'

export interface Palette {
  bg: string // 页面底色
  surface: string // 卡片、面板
  surface2: string // 次级面板、悬停
  border: string
  borderStrong: string
  text: string
  text2: string // 次要文字
  text3: string // 提示、占位
  primary: string
  primaryHover: string
  primarySoft: string // 主色的浅底（选中、用户消息）
  primaryText: string // 浅底上的主色文字
  success: string
  successSoft: string
  warning: string
  warningSoft: string
  danger: string
  dangerSoft: string
  codeBg: string
  codeText: string
  figureBg: string // 图片预览的底色：Agent 画的图表默认按白底设计，暗色模式下也放在白底上
  shadow: string
  shadowLg: string
  chart: string[] // 图表色板：顺序固定，同一指标在各图里颜色一致
}

// 中性色用锌灰（略带冷调），主色靛蓝：企业场景里稳重、在白底和深底上都清楚
export const PALETTES: Record<Mode, Palette> = {
  light: {
    bg: '#f6f6f7',
    surface: '#ffffff',
    surface2: '#f4f4f5',
    figureBg: '#ffffff',
    border: '#e4e4e7',
    borderStrong: '#d4d4d8',
    text: '#18181b',
    text2: '#52525b',
    text3: '#a1a1aa',
    primary: '#4f46e5',
    primaryHover: '#4338ca',
    primarySoft: '#eef2ff',
    primaryText: '#4338ca',
    success: '#16a34a',
    successSoft: '#f0fdf4',
    warning: '#d97706',
    warningSoft: '#fffbeb',
    danger: '#dc2626',
    dangerSoft: '#fef2f2',
    codeBg: '#18181b',
    codeText: '#f4f4f5',
    shadow: '0 1px 2px rgba(24, 24, 27, 0.04), 0 1px 3px rgba(24, 24, 27, 0.06)',
    shadowLg: '0 8px 24px rgba(24, 24, 27, 0.08), 0 2px 6px rgba(24, 24, 27, 0.04)',
    chart: ['#4f46e5', '#a5b4fc', '#0ea5e9', '#10b981', '#f59e0b'],
  },
  dark: {
    bg: '#0c0c0f',
    surface: '#151519',
    surface2: '#1d1d22',
    figureBg: '#ffffff',
    border: '#2a2a31',
    borderStrong: '#3a3a43',
    text: '#f4f4f5',
    text2: '#a1a1aa',
    text3: '#71717a',
    primary: '#818cf8',
    primaryHover: '#a5b4fc',
    primarySoft: 'rgba(129, 140, 248, 0.14)',
    primaryText: '#c7d2fe',
    success: '#4ade80',
    successSoft: 'rgba(74, 222, 128, 0.12)',
    warning: '#fbbf24',
    warningSoft: 'rgba(251, 191, 36, 0.12)',
    danger: '#f87171',
    dangerSoft: 'rgba(248, 113, 113, 0.12)',
    codeBg: '#09090b',
    codeText: '#e4e4e7',
    shadow: '0 1px 2px rgba(0, 0, 0, 0.4)',
    shadowLg: '0 8px 24px rgba(0, 0, 0, 0.5)',
    chart: ['#818cf8', '#4f46e5', '#38bdf8', '#34d399', '#fbbf24'],
  },
}

export const FONT_FAMILY =
  "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif"
export const MONO_FONT = "'SF Mono', 'JetBrains Mono', Menlo, Consolas, monospace"
export const RADIUS = { sm: 6, md: 8, lg: 12 }

/** Ant Design Vue 的主题配置：组件库用自己的算法生成悬停、激活等派生色，我们只给基础 token */
export function antdTheme(mode: Mode) {
  const p = PALETTES[mode]
  return {
    algorithm: mode === 'dark' ? antdThemes.darkAlgorithm : antdThemes.defaultAlgorithm,
    token: {
      colorPrimary: p.primary,
      colorSuccess: p.success,
      colorWarning: p.warning,
      colorError: p.danger,
      colorInfo: p.primary,
      colorBgLayout: p.bg,
      colorBgContainer: p.surface,
      colorBgElevated: p.surface,
      colorBorder: p.borderStrong,
      colorBorderSecondary: p.border,
      colorText: p.text,
      colorTextSecondary: p.text2,
      colorTextTertiary: p.text3,
      fontFamily: FONT_FAMILY,
      fontSize: 14,
      borderRadius: RADIUS.md,
      borderRadiusSM: RADIUS.sm,
      borderRadiusLG: RADIUS.lg,
      controlHeight: 34,
      wireframe: false,
    },
  }
}

const VAR_NAMES: Record<Exclude<keyof Palette, 'chart'>, string> = {
  bg: '--an-bg', surface: '--an-surface', surface2: '--an-surface-2', border: '--an-border',
  borderStrong: '--an-border-strong', text: '--an-text', text2: '--an-text-2', text3: '--an-text-3',
  primary: '--an-primary', primaryHover: '--an-primary-hover', primarySoft: '--an-primary-soft',
  primaryText: '--an-primary-text', success: '--an-success', successSoft: '--an-success-soft',
  warning: '--an-warning', warningSoft: '--an-warning-soft', danger: '--an-danger', dangerSoft: '--an-danger-soft',
  codeBg: '--an-code-bg', codeText: '--an-code-text', figureBg: '--an-figure-bg', shadow: '--an-shadow', shadowLg: '--an-shadow-lg',
}

/** 把当前模式的颜色写到 :root 的 CSS 变量上；data-theme 供少数需要区分的样式使用 */
export function applyCssVars(mode: Mode, root: HTMLElement = document.documentElement) {
  const p = PALETTES[mode]
  for (const [key, name] of Object.entries(VAR_NAMES)) root.style.setProperty(name, p[key as keyof typeof VAR_NAMES])
  root.style.setProperty('--an-font', FONT_FAMILY)
  root.style.setProperty('--an-mono', MONO_FONT)
  root.dataset.theme = mode
  root.style.colorScheme = mode // 让原生控件（滚动条、日期选择）也跟着变
}
