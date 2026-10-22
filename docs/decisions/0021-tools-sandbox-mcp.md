# 0021 - 阶段 4.2：工具、沙盒执行与 MCP 接入

**状态**：已决定（2026-10-22）。用户确认三项均按建议：独立 sandbox 服务独占 Docker socket、MCP 网关 + 出口代理、Skills 用 Markdown + `load_skill` 按需加载。

**背景**：阶段 4 要让 Agent“干活”：执行代码、读写文件、联网获取资料，并通过 MCP 接入现成的工具服务。这些能力的共同点是：**执行的内容由模型决定，而模型会被输入影响**——用户的问题、知识库里的文档、网页内容里，都可能藏着提示注入。所以这一步的核心是安全边界，不是功能。

实验脚本：`backend/eval/sandbox_exp/`（容器隔离与 Docker socket 风险）、`backend/eval/mcp_exp/`（MCP 接入、SSRF、出口代理）。实验不调用模型，零费用。

## 一、威胁模型（先说清楚防什么）
- **攻击者能控制的**：发给 Agent 的文字，以及 Agent 读到的内容（知识库文档、网页）。提示注入可以让模型调用任何它能用的工具，参数由攻击者决定。
- **要保护的**：
  - 宿主机；
  - 内部服务——Redis、Qdrant、Neo4j 在内网**没有鉴权**，能连上就能读写、清空；
  - 密钥（数据库密码、模型 API key）；
  - 别的用户的工作目录。
- **原则**：模型能触发的执行，都放在“即使完全被控制，也碰不到以上东西”的地方；边界由配置强制，不靠提示词约束模型。

## 二、实验
### 1. 加固后的容器能挡住什么（`sandbox_exp/isolation.py`）
配置：断网、只读根文件系统、`/tmp` 用 tmpfs、nobody 用户、去掉全部 capability、`no-new-privileges`、最多 64 个进程、256MB 内存（禁 swap）、0.5 核、超时 kill；工作目录单独挂载。

| 恶意 / 失控的代码 | 结果 |
|---|---|
| 正常计算、写工作目录 | 成功 |
| 写根文件系统 | `Read-only file system` |
| 访问外网 / 宿主机 / 内部服务 | `Network is unreachable` / 解析不到 |
| 提权（setuid 0） | `Operation not permitted`（uid 65534） |
| 找 Docker socket | 不存在 |
| fork 炸弹 | 第 63 个进程后 `Resource temporarily unavailable` |
| 申请 1GB 内存 | 被 OOM 杀掉 |
| 死循环 | 5 秒超时被 kill |

**延迟**：每次新建容器（创建、启动、等待、删除）中位数 **126ms**；复用一个容器用 exec 是 41ms。差距不大，所以**每次调用都新建一个干净的容器**：不会残留进程，也不存在上一次调用影响下一次的问题。同一个 run 的文件放在工作目录里，跨调用保留。

### 2. 谁能创建容器（`sandbox_exp/socket_risk.py`）
- 持有 Docker socket 的程序，可以创建**特权容器、挂载宿主机根目录**。实验里一行代码就以 root 身份读到了 Docker 数据目录。所以“把 `/var/run/docker.sock` 挂进 worker”等于让 worker 拥有 Docker 所在机器的 root 权限——而 worker 里跑着受模型输出影响的逻辑。
- 常见的“socket 代理”只能按 API 类别放行（例如允许创建容器），**管不了参数**（禁止特权、禁止挂载根目录），挡不住上面这种用法。
- **worker 自己也在容器里**：它给 Docker 传的挂载路径，会被解释成宿主机路径，不是 worker 看到的路径。用命名卷的子目录挂载（volume subpath，Docker SDK 7.2 支持）可以解决：每个 run 只挂自己的子目录，实验里访问 `../other.txt` 失败。

### 3. MCP 接入与 SSRF（`mcp_exp/`）
- 用 `langchain-mcp-adapters` 0.3.2 通过 stdio 接入官方参考实现 `mcp-server-fetch`：连接并列出工具 306ms，工具是 `fetch(url, max_length, start_index, raw)`，抓取 example.com 用了 6.4 秒。
- **SSRF**：让它抓 `http://localhost:6333/collections`，**直接拿到了 Qdrant 的集合列表**。如果它和 worker 处在同一个网络里，就能被诱导去读写内部服务。
- **只换网络不够**：放到一个不共享 compose 网络的容器里，服务名解析不到了，但 `host.docker.internal:6333`（宿主机映射出来的端口）**仍然可达**。
- **出口代理（Squid）可以解决**：工具所在的网络设为 `internal`（本身不通外网），只能经过代理出网；代理按**解析后的 IP** 拒绝私有、回环、链路本地等地址段。

  | 请求 | 结果 |
  |---|---|
  | 直连外网 | 不通 |
  | 经代理访问 example.com | 可达 |
  | 宿主机上的 Qdrant / 云元数据地址 / localhost | 403 |
  | 外网页面重定向到内网 | 第二跳 403 |
  | 真实的 MCP fetch 服务 + `--proxy-url` | 外网正常，内网 403 |

- **两个意外发现**：
  - **依赖在运行时执行 `npm install`**：fetch 服务的依赖 `readabilipy` 检测到机器上有 Node.js，就会自动执行 `npm install`，npm 的输出混进了 stdio 协议流（一条 JSON 解析错误），而且这是在运行时从 npm 下载代码，绕过了锁文件。镜像里不装 Node 时，它会退回纯 Python 解析。所以第三方 MCP 服务要跑在**构建时装好依赖、只读的专用镜像**里。
  - **`mcp` 2.x 与适配库不兼容**：`mcp` 的 Python SDK 刚发布了不兼容的 2.x，`langchain-mcp-adapters` 和 `mcp-server-fetch` 都要求 `mcp<2`。锁文件会解析到 1.30.0。
- **好消息**：MCP 的 stdio 客户端默认只把 `HOME`、`PATH`、`USER` 等 5 个环境变量传给子进程，不会把数据库密码、API key 传给第三方工具服务。

### 实验中踩的坑
- Squid 以 `proxy` 用户运行，没权限写 `/dev/stdout`，我配置的 `access_log stdio:/dev/stdout` 让它启动即退出。第一轮探测里所有请求都因为“找不到代理”而失败，**看起来像“全部拦下了”**——是从 `No such container` 发现的。脚本改为探测前先确认代理在运行。

## 三、方案
### 1. 沙盒：谁持有 Docker socket
| | A. 独立的 sandbox 服务（推荐） | B. worker 直接持有 socket（路线图原计划） |
|---|---|---|
| 结构 | 一个约 150 行的小服务独占 socket，只开放窄接口：“在某个 run 的工作目录里，用固定的加固参数执行一段代码”，以及读写该工作目录里的文件。worker 通过内网 HTTP 加令牌调用 | worker 用 Docker SDK 直接创建容器 |
| worker 被控制时 | 只能让它“执行代码”，参数由 sandbox 服务写死，拿不到特权容器 | 可以创建任意容器 → 宿主机 root |
| 代价 | 多一个服务、一次 HTTP 往返（毫秒级） | 最简单 |

### 2. 联网工具与 MCP 服务放在哪
| | A. MCP 网关 + 出口代理（推荐） | B. worker 以子进程方式启动 MCP 服务，只靠 `--proxy-url` 走代理 | C. 不设限 |
|---|---|---|---|
| 结构 | `mcp-gateway` 容器以子进程方式运行配置好的 stdio MCP 服务，对 worker 提供 streamable HTTP；它只接入一个 `internal` 网络，经 Squid 出网 | 子进程在 worker 容器里，代理只约束“会遵守代理设置的”请求 | — |
| 第三方 MCP 服务被投毒 / 有漏洞时 | 碰不到内部服务和 worker 的文件 | 和 worker 同一个网络、同一个文件系统：可以直连没有鉴权的 Redis、Qdrant、Neo4j | 同 B，且能任意访问内网 |
| 代价 | 两个小服务（网关约 80 行 + Squid，约 50MB） | 少两个服务 | — |

### 3. Skills：按需加载的工具配置
- 每个 skill 是一个 Markdown 文件（`skills/<名称>.md`）：开头写名称、一句话描述、允许使用的工具，正文是完整的操作说明。
- Agent 的系统提示里只放 skill 列表（名称 + 描述），用 `load_skill(name)` 工具取回完整说明，并在本次执行里启用它允许的工具（渐进式加载：不让所有说明一直占着上下文）。
- 示例：`data-analysis`（使用 python_exec 与文件工具）、`web-research`（使用 fetch 与知识库检索）。

### 4. 第一批工具
- `python_exec(code)`：在沙盒里执行 Python，返回 stdout、stderr 和退出原因（超时 / 内存不足）。镜像预装 pandas、matplotlib，构建时装好、版本锁定。
- `read_file` / `write_file` / `list_files`：只作用于当前 run 的工作目录（路径校验 + volume subpath 双重限制）。
- `fetch(url)`：来自 MCP fetch 服务（经网关和出口代理）。
- `knowledge_search`：已有（0018）。
- 工具输出要截断（例如 8k 字符），避免把超大输出塞进上下文。每次工具调用都记录日志和事件，前端显示“正在执行代码”。

## 确认结果
1. 沙盒：独立的 sandbox 服务独占 Docker socket，对 worker 只开放窄接口。
2. 联网工具：MCP 网关（只接入 internal 网络）+ Squid 出口代理。
3. Skills：Markdown 文件 + `load_skill` 按需加载。

## 四、实现（2026-10-22）
| 部件 | 代码 | 要点 |
|---|---|---|
| sandbox 服务 | `sandbox/app.py`、`runner.py`、`files.py` | 唯一持有 Docker socket；令牌鉴权（`/health` 除外）；每次执行新建加固容器；按“卷 + 子目录”挂载工作空间；路径校验拒绝绝对路径、`..`、NUL 和指向外部的符号链接 |
| worker 侧客户端 | `sandbox/client.py`、`agents/tools/sandbox.py` | 工作空间 id 拼进 URL 前先校验为 UUID；模型给的文件路径只放在查询参数 / 请求体里 |
| MCP 网关 | `mcp_gateway/app.py`、`docker/mcp-gateway.Dockerfile` | mcp SDK 的 lowlevel Server + 无状态 streamable HTTP；每次工具调用新起一个上游子进程；参数按上游的 inputSchema 校验；单次 60 秒超时；镜像无 Node、只读、nobody |
| worker 侧 MCP | `agents/tools/mcp.py` | 直接用 mcp SDK 客户端；网关不可用时没有联网工具，60 秒后重试 |
| 出口代理 | `docker/squid.conf` | 与实验相同的规则 |
| Skills | `agents/skills.py`、`backend/skills/*.md` | `data-analysis`、`web-research`；技能列出的工具不可用时不出现 |
| 图 | `agents/buildin/chatbot/graph.py` | 工具只在加载了列出它的技能后才绑定给模型，执行前再检查一次；工具失败以错误结果告诉模型；结果截断到 8000 字符 |
| 前端 | `web/src/composables/pendingReply.ts` | 按工具名显示“正在执行代码：…”“正在抓取网页：…” |

**网络与密钥**（compose）：
- `sandbox` 网络（internal）：只有 worker 和 sandbox 服务，所以 MCP 网关里的第三方代码连不到持有 socket 的服务。
- `tools` 网络（internal）：worker、mcp-gateway、egress-proxy。
- `egress` 网络：只有 egress-proxy 接入，用来出外网。
- sandbox 服务只拿 `SANDBOX_TOKEN`；网关和代理不拿任何密钥。

**实现时相对草稿的调整**（都是更具体的决定，不改变上面三个选择）：
1. **工作空间按对话划分，而不是按 run**：同一对话的后续提问（“再画个图”“换成柱状图”）要用到上一轮的文件。安全边界不变：一个对话只属于一个用户，id 由服务端放进图的配置，模型的参数里没有它。
2. **网关每次调用新起子进程**：调用之间不共享任何状态，一个用户的调用影响不到下一个用户；上游崩溃也不需要“重连”。代价是每次多一次进程启动——compose 里抓 example.com 全程 0.7 秒，可以接受。
3. **不用 langchain-mcp-adapters**：实验时用它接入；实现时我们的工具层是自己的 `AgentTool`，不需要它转换成 LangChain 的 BaseTool，它多出来的内容块转换和错误包装反而要再拆。直接用 mcp SDK 的客户端约 60 行。
4. **工具由技能启用**：没加载技能时模型只看到 `load_skill`（和关联知识库时的 `knowledge_search`）。工具定义本身也算输入 token；更重要的是，执行前还会再检查“这个工具是否被已加载的技能启用”——被注入的内容诱导调用没启用的工具，会直接被拒绝。
5. **工具轮数上限**：加载技能后提高到 8 轮（数据分析要“写文件 → 执行 → 看报错 → 修改 → 再执行”）；没有技能时仍是检索的 3 轮，兜底的步数上限也跟着收紧。

## 五、验证
- **自动化测试**：后端 243 个（新增沙盒 6 个，用本机真实 Docker；MCP 网关 6 个，上游是真实的 stdio 子进程；工具与技能 12 个），前端 42 个。
- **变异检查 7 项全部被发现**：去掉“执行前检查工具是否启用”、去掉“技能的工具都可用才显示”、去掉“加载技能后提高上限”、去掉客户端的工作空间 id 校验、网关把配置里的 env 传给子进程、去掉网关的调用超时、去掉“没有工作空间就拒绝”。
- **compose 里的隔离检查**（不调用模型）：

  | 检查 | 结果 |
  |---|---|
  | 写 CSV → pandas 计算 + matplotlib 画图 | 成功，1.5 秒（含导入和画图） |
  | 另一个对话的工作空间 | 看不到文件；`../<另一个对话>/…` 被拒（400） |
  | 沙盒里连 postgres、找 Docker socket、写根文件系统 | 解析不到 / 不存在 / 只读 |
  | 沙盒里的环境变量 | 没有任何数据库、模型密钥或令牌 |
  | fetch 外网（example.com） | 0.7 秒 |
  | fetch qdrant、host.docker.internal、云元数据地址、127.0.0.1、sandbox | 全部被代理拒绝（Squid 访问日志 `TCP_DENIED/403`） |
  | 网关容器直连 qdrant、postgres、sandbox、外网 | 全部解析不到 / 网络不可达；容器只读、以 nobody 运行、没有 Node |

- **真实 DeepSeek 端到端**：
  - 数据分析：模型 `load_skill` → `list_files` + `write_file` → 三次 `python_exec` → 给出总额 525、最高 2 月，并保存了折线图。6 次模型调用，输入 8.2k token（缓存命中 6.3k），输出 1.0k。
  - 网页：`load_skill` → `fetch`，4.1 秒，回答附出处。3 次模型调用。
  - 两个问题我误把脚本多跑了一遍，4 次合计约 $0.005。
- **已知局限**：
  - 沙盒镜像没有中文字体，图上的中文会显示成方框（模型自己改用了英文标签）；
  - 生成的文件还不能在网页上下载（api 不直接访问 sandbox 服务，需要一个经过鉴权的下载接口）；
  - 删除对话时还不会清理它的工作空间；
  - MCP 服务暂时只接了 fetch，网关不接受需要密钥的服务（配置里的 env 被忽略）。
