# 开发日志

按时间顺序记录每一步**做了什么、为什么这么设计、备选方案与取舍、遇到的问题与解法**。与其他文档的分工：`decisions/` 只记重大决策的结论；`interview-prep.md` 按模块整理成"面试怎么讲"；本文件保留过程，尤其是排错过程，因为"遇到过什么问题、怎么定位的"是最有说服力的项目经历。

条目模板：目标 → 设计思路 → 备选与取舍 → 遇到的问题 → 验证 → 面试可讲点。

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
5. **一个我不确定的点：在 `session.begin()` 块里手动 `rollback()` 再退出，是否安全？** 没有凭印象判断，写了测试 `test_complete_only_from_running_and_leaves_no_orphan`，验证"run 不在 running 时，助手消息整体回滚、不留孤儿"，通过。
6. **验证 compose 时误判**：执行 `up -d --build` 后只 `sleep 10`，并用 `grep` 过滤了输出，看到 api 还是旧容器、`migrate` 不在列表里，一度以为没生效。实际是镜像还在构建，grep 又把关键信息过滤掉了。重新查看完整输出后确认 `migrate` 退出码 0。教训：验证时看完整输出和状态，不要过早过滤；耗时操作要等它真正结束。

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
