# 安全说明

AgentNexus 是一个求职用的个人项目，按“企业内部部署”的威胁模型设计和测试。本文写清楚防的是什么、每道防线在哪里、哪些没防住。每一条后面都给出了对应的决策记录和测试，可以逐条核对。

## 报告问题

请在 GitHub 上通过 **Security → Report a vulnerability**（私密报告）提交，不要开公开的 issue。附上复现步骤和影响范围。这是个人项目，没有赏金，但每个报告都会认真看、修复后在提交说明里致谢。

## 威胁模型

| 谁 | 能做什么 | 我们要保证 |
|---|---|---|
| 互联网上的匿名访问者 | 访问登录页、猜密码 | 猜不动；看不到任何数据 |
| 已登录的普通用户 | 上传任意文件、让 Agent 执行代码和联网、构造任意请求 | 看不到别人和别的部门的数据；上传的文件和执行的代码伤不到服务本身 |
| 文档或网页里的提示注入 | 让模型调用工具、输出任意内容 | 工具能做的事本身是受限的；模型输出在浏览器里不会被当成代码执行 |
| 依赖的第三方包 | 存在已知漏洞 | 及时发现；影响我们的尽快修 |

不在范围内：拿到宿主机权限的攻击者、恶意的管理员、数据库或 Redis 被直接访问（它们都不对外暴露端口）。

## 防线一览

### 账号与会话（[decisions/0023](docs/decisions/0023-auth-tenancy-usage.md)）
- 密码用 argon2id（OWASP 推荐参数）哈希，在线程池里计算；用户不存在时也做一次同样耗时的校验，响应时间不泄露用户名是否存在（实测中位数 14.0ms 对 14.2ms）。
- 会话令牌放在 HttpOnly、SameSite=Lax 的 Cookie 里，脚本读不到；数据库只存令牌的 SHA-256。退出、禁用用户、重置密码都让会话立刻失效。
- CSRF：所有写请求校验 Origin 与 Host 一致。

### 登录限流（[decisions/0025](docs/decisions/0025-security-hardening.md)）
- 同一账号同一 IP 15 分钟内 5 次、同一 IP 15 分钟内 30 次，超过返回 429（带 `Retry-After`），此时不再校验密码。
- 不按“账号”单独锁，攻击者锁不住别人的账号。
- 先计数再校验，并发请求不能同时通过检查；计不了数（Redis 不可用）时拒绝登录，不放行。
- 客户端 IP 取 Nginx 用 `$remote_addr` 覆盖的 `X-Real-IP`，客户端自己带的同名头不会被采信。
- 实测：限流前一个真实账号每秒能试 217 次；限流后 50 个并发请求攻击，200 次里正好 5 次进入密码校验。

### 数据隔离（[decisions/0023](docs/decisions/0023-auth-tenancy-usage.md)）
- 两层：服务层按当前用户检查归属；PostgreSQL 行级安全（RLS）在数据库里再挡一次，漏写一处检查也不会泄露。
- 三个数据库角色：api 用的角色受 RLS 约束（NOBYPASSRLS），worker 用的角色按任务归属取数据，迁移用表所有者。
- 不存在和无权访问返回同样的 404，不泄露 id 是否存在。

### 上传的文件（[decisions/0015](docs/decisions/0015-knowledge-ingestion-entry.md)、[0025](docs/decisions/0025-security-hardening.md)）
- 只接受 .txt / .md / .pdf / .docx，有大小上限；原件存在数据卷里，不进数据库。
- PDF 和 Word 在**子进程**里解析，有超时（60 秒）和内存上限（1024MB）。构造的恶意文件最多让这一篇文档失败，不会卡死入库流水线、不会拖垮 worker（实测：死循环 3 秒被终止，内存炸弹 0.5 秒被拦下）。

### 代码执行（[decisions/0021](docs/decisions/0021-tools-sandbox-mcp.md)）
- 只有 sandbox 服务持有 Docker socket（持有 socket 等于宿主机 root），api 和 worker 都没有，调用要带令牌。
- 每次执行一个一次性容器：断网、只读根文件系统、非 root 用户、去掉所有 capabilities、`no-new-privileges`、进程数 / 内存 / CPU / 时间上限。11 个逃逸和资源耗尽用例全部挡住。

### 联网工具（[decisions/0021](docs/decisions/0021-tools-sandbox-mcp.md)）
- fetch 工具运行在 MCP 网关里，网关在 internal 网络中，只能经 Squid 出口代理上网。
- Squid 拒绝所有私有地址、本机地址、云元数据地址，只允许 80 / 443 端口：模型被诱导去访问内部服务（SSRF）时会被拦下。

### 模型输出
- 前端渲染 Markdown 时关闭原始 HTML（`html: false`），模型输出的标签当文本显示，不会被执行。
- markdown-it 升级到 15.0.3，修复了两个“构造的输入导致渲染时间平方级增长”的问题。

### 密钥
- 只放在 `.env`（不进版本库，镜像构建上下文也不包含）；每个服务只拿到自己需要的密钥。
- 前端拿不到任何模型密钥，所有模型调用都在服务端。

### 依赖（[decisions/0016](docs/decisions/0016-ingestion-pipeline-and-vector-index.md)、[0025](docs/decisions/0025-security-hardening.md)）
- 锁文件固定所有版本；新版本发布满两周才采用（防供应链投毒）。
- 例外：有已知漏洞且影响我们的包，升级到修复版本，即使不满两周。原因逐条写在 `backend/requirements.txt`。
- CI 每次运行都对三个 Python 锁文件跑 pip-audit、对前端跑 npm audit。核对过不影响我们的漏洞写在 `scripts/audit-allowlist.json`，每条有复查日期，过期 CI 就失败。

## 已知局限

- **本机直连 api 端口可以伪造客户端 IP。** compose 把 api 的 8000 端口绑定在 127.0.0.1 上供调试，api 信任 `X-Real-IP`。生产部署时不要对外暴露 api 端口，只经 Nginx 访问。
- **没有多因素认证。** 限流只防在线猜测，不防撞库后用正确的密码登录。
- **分布式猜测只能被减慢。** 大量 IP 各猜几次时，只有“账号 + IP”的上限起作用。按账号全局计数再加验证码可以缓解，但会引入“锁住别人账号”的问题，没有做。
- **Redis 故障时无法登录。** 这是“计不了数就拒绝”的代价；api 本来就依赖 Redis（事件流、取消广播）。
- **内存上限只在 Linux 上生效。** macOS 上开发时，解析子进程只有超时这一层。
- **Cookie 的 Secure 标志默认关闭。** 本地 http 演示需要；部署在 HTTPS 后面时把 `COOKIE_SECURE` 设为 true。
- **提示注入没有根治。** 能做的是限制工具的能力（断网的沙盒、只能访问公网的 fetch、按技能启用工具），而不是识别注入。
- **没有删除知识库和文档的接口。** 数据只能由管理员在数据库里清理。
