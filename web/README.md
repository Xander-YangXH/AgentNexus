# AgentNexus 前端

Vue 3 + Vite + Ant Design Vue。设计说明见 [decisions/0008](../docs/decisions/0008-frontend-integration.md)，整条链路的走读见 [学习导读](../docs/guides/message-lifecycle.md)。

## 本地开发

先在仓库根目录启动后端（`docker compose up -d`），然后：

```bash
cd web
npm ci            # 严格按 package-lock.json 安装
npm run dev       # http://localhost:5173 ，/api 由 Vite 代理到 localhost:8000
```

想在页面上看清流式效果又不消耗模型额度：在根目录 `.env` 里设置 `LLM_PROVIDER=fake`、`LLM_FAKE_STREAM_DELAY=0.1`，再 `docker compose up -d api`。

## 测试与构建

```bash
npm test          # vitest：SSE 解析与重连、回复状态机、Markdown 安全
npm run typecheck # vue-tsc 类型检查
npm run build     # 类型检查 + 打包到 dist/
```

## 代码地图

| 文件 | 作用 |
|---|---|
| `src/api/http.ts` | 普通 HTTP 请求；统一带 `X-User-Id` 头（鉴权占位） |
| `src/api/sse.ts` | **SSE 客户端**：`SseParser` 按规范解析字节流；`streamRunEvents` 负责重连与 `Last-Event-ID` 续传 |
| `src/composables/pendingReply.ts` | “正在生成的回复”状态机（纯函数）：waiting → streaming → completed / failed |
| `src/composables/useChat.ts` | 页面状态与动作：选用户、选对话、发消息、订阅事件、刷新后接上正在生成的回复 |
| `src/utils/markdown.ts` | 渲染模型输出，禁用原始 HTML（防 XSS） |
| `src/components/*.vue` | 用户选择、对话列表、聊天面板（含“等待首字 N 秒”和“生成中断”） |
