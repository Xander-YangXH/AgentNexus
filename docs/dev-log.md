# 开发日志

按时间顺序记录每一步**做了什么、为什么这么设计、备选方案与取舍、遇到的问题与解法**。与其他文档的分工：`decisions/` 只记重大决策的结论；`interview-prep.md` 按模块整理成"面试怎么讲"；本文件保留过程，尤其是排错过程，因为"遇到过什么问题、怎么定位的"是最有说服力的项目经历。

条目模板：目标 → 设计思路 → **技术选型明细（选了什么、为什么、否掉了什么）** → 备选与取舍 → 遇到的问题 → 验证 → 面试可讲点。

## 问题索引（按发生顺序，随开发追加）

| # | 阶段 | 问题 | 根因（一句话） |
|---|---|---|---|
| 1 | 0 | demo yml 报 `mapping values are not allowed` | shell 命令里的 `: ` 被 YAML 当键值分隔，引号嵌套又冲突 → 改列表形式 |
| 2 | 0 | Docker daemon 未启动，里程碑无法验证 | 环境缺失；把"已验证/未验证"分开写，环境就绪后补测并加故障用例 |
| 3 | 0 | `git commit` 失败：`HEAD.lock` 已存在 | 10 月 14 日遗留的 0 字节锁文件；确认无 git 进程后删除 |
| 4 | 1 | `Column object 'id' already assigned to Table` | `mapped_column` 对象被多个模型共用，列对象只能属于一张表 |
| 5 | 1 | 测试夹具 `Could not parse SQLAlchemy URL` | `make_conninfo` 返回 `key=value` 串不是 URL → 改用 `make_url().set(database=)` |
| 6 | 1 | macOS `sed -i` 报 `invalid command code` | BSD sed 要求 `-i ''` |
| 7 | 1 | Alembic 警告 `No path_separator found` | 新版本弃用旧的路径分隔推断 → 配置 `path_separator = os` |
| 8 | 1 | 不确定"事务内手动 rollback 再退出块"是否安全 | 不凭印象，写集成测试验证（通过） |
| 9 | 1 | compose 验证误判：以为 `migrate` 没生效 | `sleep` 太短 + `grep` 过滤掉关键输出；镜像其实还在构建 |
| 10 | 1 | `TypeError: 'function' object is not subscriptable` | 类里方法名 `list` 遮蔽了内置 `list`，后面的 `list[Message]` 取到方法 |
| 11 | 1 | （预防）Alembic 的 `sqlalchemy.url` 含 `%` 会被 ConfigParser 当插值 | 设置时把 `%` 转义成 `%%`；真实密码常含 `%` 等特殊字符 |
| 12 | 1 | 多步脚本 `cd ../../..` 退多一层，后半段 `FileNotFoundError`，改动处于一半状态 | 靠相对 `cd` 切目录；先 `git status` 查清再补跑，脚本里用绝对路径 |
| 13 | 1 | 新测试一次全过，不确定是否有效 | 做变异检查：故意破坏实现，确认测试会失败；还证实 `_pending` 只是优化 |
| 14 | 1 | 文档脚本中途崩溃（正则转义写错，统计为 0 → 除零），但后面的 `git commit` 照常执行，提交了“改了一半”的文档 | 脚本与提交没有串成“前一步成功才继续”；再次犯了第 12 条同类错误。补救：查清已写/未写，补一个修复提交 |

---

## 2026-10 · 阶段 0：环境与交付物

### 目标
一条 `docker compose up` 起 Postgres + Redis + FastAPI，`/health` 返回 200，新人 clone 即可复现。

### 设计思路
- **依赖就绪靠 healthcheck，不靠 sleep 或重试**。`depends_on` 默认只等容器启动，而 Postgres 进程起来到真正接受连接之间有窗口期。给 postgres（`pg_isready`）、redis（`redis-cli ping`）配 healthcheck，api 用 `condition: service_healthy` 等待。
- **存活与就绪探针拆开**。`/health` 不查依赖：依赖挂了重启 api 也没用，反而可能引发重启风暴。`/health/ready` 才真连 Postgres 和 Redis，失败返回 503，并带上是哪个依赖挂了。
- **配置全走环境变量**。`.env` 不提交，提交 `.env.template`；连接串在 compose 里用服务名拼装，应用代码只读 `DATABASE_URL`、`REDIS_URL`。
- **宿主机端口只绑 127.0.0.1**，避免开发机上的数据库被局域网访问。
- **Dockerfile 先装依赖再拷代码**，改业务代码时依赖层缓存不失效；uvicorn 监听 `0.0.0.0`，否则端口映射后宿主机访问不到。

### 技术选型明细
| 选了什么 | 为什么 | 否掉了什么 / 代价 |
|---|---|---|
| `postgres:16-alpine` | 16 是稳定的主流版本，与 `learning/langgraph` 练习用的版本一致，行为可对照；alpine 镜像小，拉取快 | 17 太新、生态跟进不全；`pgvector` 镜像暂不需要（向量库选 Qdrant）。alpine 用 musl，排序规则（collation）与 Debian 镜像略有差异，生产环境建议换 Debian 系镜像 |
| `redis:7-alpine` + `--appendonly yes` | 后续要放队列信号、事件流，开 AOF 持久化，重启不丢最近写入 | 默认只有 RDB 快照，崩溃会丢最近几分钟数据 |
| `python:3.12-slim`（而不是 alpine） | 依赖 `psycopg-binary`、`uvloop` 等带 C 扩展的包，glibc 的 slim 镜像有预编译 wheel；alpine（musl）常没有 wheel，需要现场编译，慢且易失败 | 镜像比 alpine 大。注意：本机 venv 是 Python 3.13、容器是 3.12，代码用到的 `StrEnum` 需 ≥3.11，两者都满足；若后续依赖出现版本差异再统一 |
| 单阶段 Dockerfile | 当前依赖少，先求简单可读 | 多阶段构建能缩小镜像，等镜像体积成为问题再做 |
| `uvicorn[standard]` | 带 `uvloop`（更快的事件循环）和 `httptools`（更快的 HTTP 解析） | 纯 `uvicorn` 更轻，但性能收益在 I/O 密集的 Agent 场景里值得 |
| 依赖清单用 `requirements.txt` | 对 solo 项目足够，Dockerfile 里"先拷清单再装依赖"容易利用层缓存 | `uv`/`poetry` 有锁文件、可复现性更好；依赖多起来后再迁移 |
| Compose 而不是 Kubernetes | 本地一键复现，学习与运维成本低 | K8s 的滚动发布、弹性伸缩，对简历项目的体量是过度设计 |

### 备选与取舍
- 健康检查用 `curl` 还是 Python 标准库？slim 镜像没有 curl，为一个探针装 curl 会增大镜像，所以用 `python -c "urllib.request.urlopen(...)"`。
- `/health` 要不要顺带查依赖？不查。把"进程活着"和"能提供服务"分成两个探针，各自语义清晰。

### 遇到的问题
1. **demo yml 解析失败**（`mapping values are not allowed in this context`）。原因：`command: sh -c "echo 'x: y'"` 里的 `: ` 被 YAML 当成映射的键值分隔。先试了加外层引号，内外引号又冲突；最终改成列表形式（`command: [sh, -c, "..."]`），不再依赖引号转义。教训：compose 里带冒号、引号的 shell 命令一律用列表形式。
2. **一开始 Docker daemon 没启动，无法验证里程碑**。当时只能做到 `docker compose config` 校验配置和用 TestClient 测应用代码，镜像构建与真实 `up` 没跑过。做法是把"已验证/未验证"在交接文档里分开写清楚，不把未验证的写成已完成；等 Docker 起来后在本机补测：三服务 healthy，停掉 postgres 后 `/health` 仍 200、`/health/ready` 变 503，恢复后回到 ok。
3. 仓库里残留了一个 0 字节的 `.git/HEAD.lock`，导致提交失败。先确认没有 git 进程在运行，再删除。教训：删锁文件前先确认没有持有者。

### 验证
`docker compose ps` 三个服务 healthy；两个探针行为符合预期（见上）；api 容器内能按服务名解析 postgres、redis。

### 面试可讲点
`depends_on` 与 healthcheck 的区别；存活 vs 就绪探针为什么要分；容器里的 localhost 是自己，容器间用服务名；`down -v` 会删数据卷。详见 `interview-prep.md` 的"本地开发环境"一节与 `decisions/0003`。

---

## 2026-10 · 阶段 1（一）：数据层

### 目标
把用户、对话、消息、AgentRun 落库，并实现"先落库再投递"的第一半：提交消息的同时创建一个 `queued` 的 run。

### 设计思路
- **事实来源是数据库，队列只是触发器**。run 以 `queued` 状态落库后，投递队列即使失败或进程崩溃，也可以靠扫描 `status=queued` 补发。这要求"用户消息"和"queued run"必须在**同一个事务**里提交。
- **并发正确性交给数据库，不在应用层加锁**。多个 api/worker 实例下，应用层的"先查后改"都有竞态，而数据库的约束和条件更新是串行化的：
  - 抢占执行权：`UPDATE agent_runs SET status='running' WHERE id=? AND status='queued'`，根据 `rowcount` 判断是否抢到。
  - "同一对话同一时刻最多一个活跃 run"：部分唯一索引 `WHERE status IN ('queued','running')`。原因是两个 run 并发写同一个 thread 的 checkpoint 会互相覆盖。
  - 终态不可覆盖：`finish()` 只允许从 running 转出，迟到的结果不能改写已结束的 run。
- **分层各管一件事**：`repositories` 只做单表读写、不管事务；`services` 一个方法一个事务，成功全提交、失败全回滚；表定义放 `storage/postgres`。
- **消息排序用自增 `seq`**：同一事务内 `now()` 都是事务开始时间，多条消息时间戳相同；uuid 又无序，两者都不能保证顺序。
- **迁移用独立的一次性 `migrate` 服务**：多个实例同时启动各自跑迁移会互相抢，一次性服务跑完退出，api 用 `service_completed_successfully` 等它。

### 技术选型明细
| 选了什么 | 为什么 | 否掉了什么 / 代价 |
|---|---|---|
| SQLAlchemy 2.0 异步 | 需要部分唯一索引、CHECK、条件更新这类细粒度控制，同时表会越来越多，需要模型层和迁移联动；2.0 风格（`Mapped[]` 类型注解）与 FastAPI 的类型驱动风格一致 | 直接写 SQL：灵活但表多了后重复代码和迁移同步成本高；SQLModel：封装更薄但对复杂约束支持不如原生 SQLAlchemy；Tortoise 等：生态和资料不如 SQLAlchemy |
| psycopg3 作为驱动 | LangGraph 的 `PostgresSaver` 用的就是 psycopg3，项目里只维护一套 Postgres 驱动和连接配置；同时支持同步和异步 | asyncpg 更快，但会出现两套驱动、两套连接串格式，收益不值得 |
| Alembic | 模型改了能 autogenerate 出迁移草稿，再人工审核；迁移有版本链，可回滚 | `create_all()` 只能建表不能演进已有表；纯手写 SQL 脚本没有版本管理 |
| 主键用 UUID，由数据库 `gen_random_uuid()` 生成 | id 会出现在 URL 里，自增整数可枚举、会泄露数据量；UUID 不可猜，多实例无需协调；PG13+ 内置该函数，不用装扩展 | 自增 bigint 索引更紧凑、插入更有序；UUID 索引更大。所以**需要顺序的地方（messages）单独加 `seq` 自增列**，主键仍用 UUID |
| 时间列用 `timestamptz` | 带时区，跨时区部署和展示不出错 | 不带时区的 `timestamp` 容易在服务器时区变化时产生歧义 |
| status/role 用 `String + CHECK` | 增减枚举值只需一次迁移改约束；原生 PG enum 增删值更麻烦 | 原生 enum 存储更省，但演进成本高 |
| Alembic 用同步引擎 | 迁移是一次性脚本，异步无收益，同步更简单稳妥 | 在 env.py 里跑异步引擎要额外样板代码 |
| 测试用真实 Postgres + `pytest-asyncio` | 部分唯一索引、`UPDATE … WHERE` 的 rowcount、事务回滚，SQLite 模拟不出来；测试库由真实迁移建表，顺带验证迁移脚本 | SQLite 内存库更快，但会测出"假通过"；代价是跑测试要先起 Postgres |

### 备选与取舍
- **ORM**：SQLAlchemy 异步 vs 直接写 SQL。选 SQLAlchemy，因为表和关系会随阶段 2-5 不断增加，需要迁移工具和模型层；驱动选 psycopg3，与 LangGraph 的 PostgresSaver 同一个，不维护两套驱动。代价是学习成本，且部分唯一索引、条件更新必须在真实 Postgres 上测试，SQLite 模拟不了。
- **Alembic 用同步引擎**：迁移是一次性脚本，不需要异步，同步更简单稳妥。
- **status/role 用 CHECK 约束而不是 PG 原生 enum**：枚举值后续要增减时，改 CHECK 约束只需一次迁移，原生 enum 的变更更麻烦。
- **lease 字段这一步不加**：阶段 4 才需要，提前加是臆测的设计；届时通过新迁移追加，也正好能练习"在线加列"。

### 遇到的问题
1. **`Column object 'id' already assigned to Table 'users'`**。我把 `mapped_column(...)` 的结果存在模块变量 `_uuid_pk` 里给四个模型共用。一个列对象只能属于一张表。改成函数 `_uuid_pk()`，每次新建。
2. **测试夹具里 `Could not parse SQLAlchemy URL`**。想用 `psycopg.conninfo.make_conninfo` 替换库名，但它返回的是 `key=value` 格式，不是 URL。改用 SQLAlchemy 的 `make_url(...).set(database=...)`，全程保持 URL 形式。
3. **macOS 的 `sed -i` 报 `invalid command code`**：BSD sed 要求 `-i ''`。
4. **Alembic 弃用警告**（`No path_separator found`）：在 `alembic.ini` 加 `path_separator = os`。
5. **预防性处理：Alembic 的 `sqlalchemy.url` 含 `%` 时会被 ConfigParser 当成插值语法报错**。数据库密码里出现 `%`、`@` 等字符很常见。设置到 Alembic 配置时把 `%` 转义成 `%%`（`conftest.py` 里的 `.replace("%", "%%")`）。同时统一用 `to_sqlalchemy_url()` 把应用通用的 `postgresql://` 转成 SQLAlchemy 需要的 `postgresql+psycopg://`，避免连接串格式在各处不一致。
6. **一个我不确定的点：在 `session.begin()` 块里手动 `rollback()` 再退出，是否安全？** 没有凭印象判断，写了测试 `test_complete_only_from_running_and_leaves_no_orphan`，验证"run 不在 running 时，助手消息整体回滚、不留孤儿"，通过。
7. **验证 compose 时误判**：执行 `up -d --build` 后只 `sleep 10`，并用 `grep` 过滤了输出，看到 api 还是旧容器、`migrate` 不在列表里，一度以为没生效。实际是镜像还在构建，grep 又把关键信息过滤掉了。重新查看完整输出后确认 `migrate` 退出码 0。教训：验证时看完整输出和状态，不要过早过滤；耗时操作要等它真正结束。

### 验证
- 8 个集成测试在真实 Postgres 上通过：原子提交；他人对话/不存在的对话统一报 `ConversationNotFound`；提交失败不留孤儿消息；前一个 run 结束后可以再提交；8 路并发 claim 只有 1 个成功；未 claim 不能直接 complete；失败记录错误且终态不可再改；同事务内消息顺序正确。
- compose：`migrate` 退出码 0，五张表建成，重复 `up` 迁移幂等，`/health/ready` 仍为 ok。

### 面试可讲点
为什么 submit 要同事务；为什么用条件更新和部分唯一索引而不是应用层加锁；消息为什么不能按 `created_at` 排序；迁移为什么单独成一次性服务。详见 `interview-prep.md` 的"Agent 运行链路"与 `decisions/0004`。

---

## 2026-10 · 阶段 1（二）：API 路由层

### 目标
把 `RunService` 等服务暴露成 HTTP 接口：创建用户/对话、发消息、查历史、查 run 状态。

### 设计思路
- **路由薄、服务厚**：路由只做"解析参数 → 调服务 → 返回"；业务规则（归属校验、同事务写入）在服务层。HTTP 状态码不出现在服务层：服务只抛领域异常（`ConversationNotFound`、`RunConflict`…），在 `main.py` 统一映射成 404/409。这样以后换成 worker 或 CLI 调同一套服务时不依赖 HTTP。
- **发消息返回 202 Accepted，不是 200/201**：消息和 run 已经落库，但执行是异步的。响应体直接返回 run（含 `status=queued`），客户端拿 run id 轮询或订阅进度。语义上 202 恰好是"已受理，尚未处理完"。
- **鉴权占位**：阶段 5 才做真正的 RBAC，但数据隔离从现在就要生效。用 `X-User-Id` 头 + 一个 `current_user_id` 依赖（校验用户存在，否则 401）。以后接 token 认证只替换这个依赖。路由签名里写 `CurrentUser`，不关心身份从哪来。
- **越权与不存在一视同仁**：访问别人的对话/run 返回 404，和不存在一样，不泄露 id 是否存在（返回 403 会暴露资源存在）。
- **sessionmaker 通过依赖注入**：lifespan 里创建引擎并挂到 `app.state`，路由经 `Depends` 取用；测试用 `dependency_overrides` 换成测试库，不用改任何业务代码。
- **`create_app()` 工厂**：测试可以创建独立的 app 实例。

### 技术选型明细
| 选了什么 | 为什么 | 否掉了什么 / 代价 |
|---|---|---|
| Pydantic 请求/响应模型（`schemas.py`）与 ORM 模型分开 | 对外接口字段由 schema 显式声明，数据库加列不会意外泄露到接口；校验（如内容 1~20000 字符）放在入口 | 直接返回 ORM 对象省代码，但接口和表结构耦合，改表即破坏接口 |
| `X-User-Id` 头做鉴权占位 | 数据隔离从现在起就生效，又不引入密码哈希、token 签发这些阶段 5 的内容；替换点只有一个依赖函数 | 直接上 JWT：这一步会被无关代码淹没；完全不做隔离：之后补隔离要改所有查询 |
| 依赖注入传 sessionmaker | 测试用 `dependency_overrides` 换成测试库，业务代码零改动 | 全局单例连接：测试要打补丁，且难以并行 |
| `create_app()` 工厂 | 每个测试得到独立 app 实例，互不污染 | 模块级全局 `app` 在测试间共享状态 |
| 测试用 `httpx.AsyncClient` + `ASGITransport` | 数据库夹具本身是异步的，客户端也用异步，整条链路在同一个事件循环里；不需要真的监听端口 | Starlette 的同步 `TestClient` 内部起独立线程/事件循环，与异步夹具混用容易出现跨事件循环的问题（这里是设计上避开，并非实际踩到） |
| 发消息返回 202 + run | 语义为"已受理、未处理完"，客户端用 run id 查进度 | 200/201 会让客户端误以为已处理完；同步等待 Agent 完成则会占住 HTTP 连接几十秒 |

### 备选与取舍
- 重复用户名：先查再插有竞态，改成以数据库唯一约束为准，捕获 `IntegrityError` 转成 `UserExists`（409）。和阶段 1（一）"并发正确性交给数据库"一脉相承。
- 创建用户暂不做登录/密码：这一步的目标是链路，密码哈希、token 属于阶段 5，提前做会引入大量与主线无关的代码。
- 这一步**还没有派发和执行**：发消息后 run 停在 `queued`。这是有意的，把"落库"和"派发"拆成两步，各自可单独验证。

### 遇到的问题
1. **`TypeError: 'function' object is not subscriptable`**：在 `ConversationService` 里定义了方法 `list`，类体里后面的 `list[Message]` 类型注解取到的是这个方法，而不是内置 `list`。症状在 import 时就爆，但报错位置指向的是别的方法的注解，一开始不直观。解法：方法改名 `list_for_user`。教训：不要用 `list`/`dict`/`type`/`id` 这类内置名当类里的方法名，类体内它们会遮蔽内置名。
2. 把异常类从 `runs.py` 拆到 `errors.py` 后，旧测试仍从 `runs` 导入。因为 `runs.py` 自己也导入了这些名字所以没报错，但依赖了"碰巧能导入"，所以把测试改成从 `errors` 导入。

### 验证
- 新增 6 个 API 测试（共 14 个通过）：重复用户名 409；缺失/非法/不存在的用户头 401；发消息 202 且历史可查；活跃 run 期间再发消息 409 且不留孤儿消息；两个用户互相看不到对方数据（对话列表、消息、发消息、run 都是 404）；空内容和非法 uuid 返回 422。
- 在 Compose 里用 curl 端到端走了一遍：建用户 → 建对话 → 发消息 202 → 再发 409 → 历史只有一条 → 不带头 401，api 日志无错误。

### 面试可讲点
202 的语义；为什么越权返回 404；领域异常到 HTTP 状态码的映射放在哪一层；鉴权占位怎么保证以后可替换。见 `interview-prep.md` 的"分层架构"与"Agent 运行链路"。

---

## 2026-10 · 阶段 1（三）：派发与执行（进程内单机版）

### 目标
让 `queued` 的 run 真正被执行：派发 → 抢占 → 执行 → 回写，并处理好超时、异常、崩溃、关停。用回声 Agent 打通，暂不接 LLM。

### 设计思路
- **数据库是事实来源，队列只是加速**。run 一旦以 `queued` 落库，就“必须被执行”。内存队列丢了不要紧：周期扫描 `status=queued` 补投。这样“提交成功 ⇒ 一定会被执行”这个承诺不依赖任何内存状态。
- **正确性靠 `claim`，不靠队列**。入队时可能重复（提交时入队 + 扫描补投），所以执行前一律先 `claim`（`UPDATE … WHERE status='queued'`），抢不到就跳过。`_pending` 集合只是减少重复排队的优化。我专门做了变异检查：去掉它测试依然全过，证实真正的保证来自 `claim`。
- **每个失败路径都要把 run 收尾到终态**，否则对话会被“同一对话最多一个活跃 run”的唯一索引永久锁死。覆盖了四种情况：Agent 抛异常 → failed 并记录错误；超时 → failed；优雅关停 → failed（用 `shield` 保证收尾写库不被再次取消打断）；进程被强杀 → 下次启动时恢复为 failed。
- **启动恢复只在单进程下安全**。把所有 `running` 置为 failed，多实例会互相误杀。这条假设写进了 decisions/0005 和代码注释，它就是阶段 4 需要 lease + heartbeat 的最直接理由。
- **Agent 用“函数”契约**：`RunContext -> str`。Agent 不依赖数据库，上下文由服务层构造，历史截止到本次输入消息（按 `seq`），重放结果稳定。
- **投递放在路由里 `submit` 成功之后**，而不是服务层事务内：投递是副作用，不能发生在事务提交之前，否则消费者可能读不到刚写的数据。

### 技术选型明细
| 选了什么 | 为什么 | 否掉了什么 / 代价 |
|---|---|---|
| `asyncio.Queue` + N 个消费协程 | 单进程内最简单的“生产者-消费者”，与 FastAPI 同一个事件循环，无需外部依赖 | Redis/ARQ：现在没有多 worker 的需求，过早引入会同时面对业务与分布式两类 bug；代价是进程崩溃丢内存队列（靠扫描补投兜底） |
| 周期扫描补投（默认 5s） | 把“队列丢任务”变成“最多延迟一个扫描周期”，实现成本极低 | 数据库通知（LISTEN/NOTIFY）延迟更低但复杂；5s 对聊天场景可接受，间隔可配置 |
| `asyncio.wait_for` 做超时 | 标准库，超时后会取消 Agent 协程，资源能释放 | 线程/进程级超时：对纯异步协程没必要 |
| 回声 Agent 打通链路 | 把“链路 bug”和“LLM/提示词 bug”分开排查；测试快速、确定、免费 | 直接接 LLM：慢、非确定、要密钥，出问题时分不清是谁的错 |
| 配置走环境变量并给默认值 | 沿用已有约定；并发、超时、扫描间隔不用改代码即可调 | 配置中心：对当前规模过度 |
| 集成测试 + **变异检查** | 测试一次全过时，不能默认它有效；故意破坏实现看测试会不会失败，才能证明它真的在保护行为 | 只看覆盖率：覆盖到不等于断言有效 |

### 备选与取舍
- 被打断的 run：重试还是标失败？选**标失败**，由用户重发。自动重试需要幂等保证（比如工具已产生副作用），现在没有 checkpoint，不能保证；接入 LangGraph checkpoint 后再改成断点恢复。
- 启动恢复：全部置 failed vs 重新入队？重新入队可能让一个“毒任务”（每次都让进程崩溃的 run）无限重启进程，所以选 failed。

### 遇到的问题
1. **接线脚本找不到文件**：我先 `cd ../../..` 想回到 `backend`，实际退到了仓库根，后半段修改 `server/*.py` 的脚本报 `FileNotFoundError`，而前半段（仓储、服务、派发器）已写成功，处于“改了一半”的状态。靠输出里的报错发现，先 `git status` 确认哪些已改、哪些没改，再从正确目录重跑后半段。教训：多步脚本里不要靠相对 `cd` 切换目录，写绝对路径或每步显式 `cd`；出错后先查清状态再补，不要整体重来以免重复修改。
2. **测试一次全过，反而要警惕**。8 个新测试第一次运行全部通过。为了确认它们真的有效，对实现做了 7 种变异：去掉启动恢复 / 去掉补投 / 关停时不收尾 / 并发翻倍 / 去掉超时，对应测试都按预期失败；去掉 `_pending` 去重，测试仍然通过。最后这一条不是测试缺陷，而是证明了“去重只是优化，claim 才是保证”。
3. **变异检查结论的边界**：我还试了“连 claim 判断一起去掉”，结果 7 个测试失败，但原因是 run 永远停在 `queued`（我把状态转换一并破坏了），**不能**当成“证明会重复执行”的证据，所以没有把它写成结论。
5. **文档脚本崩溃后仍提交了半成品（和第 1 点同类的失误，我再犯了一次）**。统计完成度的脚本里，正则在 `r'...'` 原始字符串里又多写了一层反斜杠，匹配 0 项，`d*100//t` 除零报错。脚本与 `git add && git commit` 是分开的两条命令，前者失败后者照样执行，于是提交里 decisions/dev-log/interview-prep 是新的，而完成度表、HANDOFF、`.env.template`、qa-log 还是旧的。发现方式：脚本输出里的 Traceback。处理：逐项检查每个文件的“标志性内容”是否已写入，只补缺的部分，再追加一个修复提交（提交未推送，本可 amend，但保留修复提交更能如实反映过程）。改进：统计脚本加 `assert t > 0`；需要“成功才提交”时把脚本和提交写进同一条 `&&` 链，或先看输出再提交。
4. **一个我暂时无法在测试里复现的点**：强杀进程（kill -9）时的恢复，只能靠下次启动的 `fail_orphaned_running`；测试里用“手工 claim 后再启动派发器”模拟了这个状态，而不是真的杀进程。

### 验证
- 22 个测试通过（原 14 个 + 派发器 8 个）。
- 容器内端到端：发消息 202 → run 变 `succeeded` → 历史里出现 `echo: …`；`docker compose restart api` 后历史仍在，继续发消息 202 且再次得到回复；api 日志无报错。

### 面试可讲点
“先落库再投递”的完整闭环；为什么队列不是事实来源；为什么正确性靠原子 claim；每个失败路径为什么必须收尾（否则对话被唯一索引锁死）；单进程假设的边界以及 lease 为什么必要；变异检查如何验证测试有效。见 `interview-prep.md` 的“Agent 运行链路”与 `decisions/0005`。
