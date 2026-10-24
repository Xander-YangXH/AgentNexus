# 前端设计系统：一份 token，浅色深色两套界面

> 阶段 6.1 的界面改版（决策 0024）。代码：`web/src/theme/`、`web/src/styles/base.css`、`web/src/components/`。
> 读完应该能回答：颜色是怎么从一个文件流到组件库、我们自己的样式和图表里的；改版为什么没有动任何状态逻辑。

## 0. 颜色从哪里来

```
theme/tokens.ts（PALETTES.light / PALETTES.dark）
   ├─ antdTheme(mode)    → <a-config-provider :theme>    → 组件库（按钮、输入框、表格……；深色用 darkAlgorithm 生成派生色）
   ├─ applyCssVars(mode) → :root { --an-bg, --an-primary, … } → 我们自己的样式（var(--an-*)），base.css 与各组件
   └─ PALETTES[mode] 直接读 → ECharts、cytoscape（画在 canvas 上，读不到 CSS 变量；切换主题时重画 / 重设样式）

theme/useTheme.ts：偏好（跟随系统 / 浅色 / 深色，存 localStorage）+ 系统的 prefers-color-scheme → mode
```

规则：业务组件里不写颜色值。要新颜色先加到 token（两套都要加，有测试检查两套的键一致）。

## 1. 页面结构
- **外壳**：左侧 64px 的全局导航栏（对话、知识库、用量、管理；下面是外观切换和账号菜单），右侧是页面。切换页面时结构不变。
- **对话**：对话列表（按今天 / 昨天 / 最近 7 天 / 更早分组，可搜索）+ 对话区。对话区有标题栏（标题、模式、关联知识库）、780px 宽的阅读列、固定在底部的输入卡片。窄窗口（< 960px）时对话列表收进抽屉。
- **其他页面**：统一用“标题区 + 内容卡片”（`.an-page`、`.an-page-header`、`.an-card`）。

## 2. 让 Agent 的工作看得见
| 状态 | 来自哪个事件 | 怎么显示 |
|---|---|---|
| 调用工具 | `tool.started` / `tool.finished` | 时间线：进行中（转圈）、完成（绿勾）、失败（红叉），后面是参数摘要（检索词、网址、代码第一行） |
| 调研子任务 | `research.subtask` | 一排小标签：进行中 / 完成 / 失败 |
| 等待审批 | `run.awaiting_approval` | 计划卡片：子任务列表、预计调用次数、批准 / 按意见修改 / 取消 |
| 回答的依据 | 消息的 citations | 编号卡片，悬停看原文 |
| 产物 | 消息的 attachments | 文件卡片 + 下载 |

这些都只是展示：状态在 `pendingReply.ts` 这个纯函数状态机里，改版只给它加了一个 `steps` 字段（工具调用列表），并补了测试。

## 3. 改版时踩到的几个问题
- **网格项的最小宽度**：网格项默认 `min-width: auto`，不换行的长文件名会把整列撑宽，手机上出现横向滚动。`grid-template-columns: minmax(0, 1fr)`。
- **选择器误伤组件内部**：`.field > span` 也命中了密码框组件最外层的 span。给自己的元素用类名，不用标签选择器。
- **部署后看到旧界面**：入口 index.html 被浏览器缓存，它引用的是旧的带哈希文件。Nginx：index.html `no-cache`，`/assets/` 长期缓存。
- **截图不可靠**：切换主题后立刻截图，会截到过渡帧。判断样式先读计算样式（`getComputedStyle`）。

## 4. 自测题
1. 组件库的颜色和我们自己写的样式，为什么能用同一份定义？深色模式下各自是怎么变的？
2. 为什么图表和图谱画布要单独处理主题？
3. 为什么界面重写后，原有的前端测试一个都不用改？
4. 手机宽度下的横向滚动是怎么定位到的？根因是什么？
5. 为什么 index.html 不能缓存，而 `/assets/` 可以缓存一年？
