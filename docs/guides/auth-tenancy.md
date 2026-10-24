# 登录、部门隔离与用量看板

> 配套《一条消息的完整旅程》《深度调研》。阶段 5 把 `X-User-Id` 占位换成真实登录，加上部门和角色，
> 在应用层检查之外再加数据库的行级安全（RLS），并做了用量看板。
> 决策记录：0023（含四个零费用实验）。代码：`security/`、`storage/roles.py`、`services/usage.py`、迁移 `0011`、`server/csrf.py`。

## 0. 一个请求怎么知道“我是谁”

```
浏览器（Cookie: agentnexus_session=<随机令牌>，HttpOnly，脚本读不到）
  │  写请求：CsrfMiddleware 检查 Origin 与 Host 一致、不是 Sec-Fetch-Site: cross-site
  ▼
current_principal 依赖：SHA-256(令牌) → user_sessions（未过期）→ users（未禁用）→ Principal(id, 角色, 部门)
  │  放进上下文变量 principal_var
  ▼
服务层：显式检查（知识库可见 / 只有所有者能写 / 对话只属于自己）——第一道防线，返回清楚的 404 / 403
  ▼
SQLAlchemy 会话开始事务 → after_begin 事件：set_config('app.user_id' / 'app.department_id' / 'app.role', ..., true)
  ▼
PostgreSQL（api 以 agentnexus_api 角色连接，受 RLS 约束）：策略用 app_user_id() 等函数过滤行——第二道防线
```

worker 以 `agentnexus_worker`（BYPASSRLS）连接：它执行的是“某个 run / 文档的任务”，按归属取数据，不是“以某个用户的身份”查询。迁移用表所有者。

## 1. 为什么是会话而不是 JWT
| | 服务端会话 + HttpOnly Cookie（选择） | JWT |
|---|---|---|
| 退出 / 禁用 / 重置密码 | 删会话，下一个请求就失效 | 过期前一直有效（要黑名单） |
| XSS | 脚本读不到令牌 | 放在 localStorage 里会被拿走 |
| 下载产物 | 普通链接即可（浏览器自动带 Cookie） | 要 fetch 成 Blob |
| 代价 | 要防 CSRF；每个请求查一次会话（主键，约 0.2ms） | 不需要 CSRF 防护 |

- **CSRF**：SameSite=Lax 让别的网站发起的跨站 POST 不带 Cookie；中间件再比较 Origin 和 Host。不带 Origin 的请求（curl、脚本）放行——CSRF 需要受害者的浏览器。
- **一个差点让所有写请求失败的细节**：Nginx 原来转发 `Host $host`，`$host` 不带端口，而浏览器的 Origin 是 `localhost:8080`。改成 `$http_host`；开发时的 Vite 代理也不能改 Host（`changeOrigin: false`）。
- **密码**：argon2id（OWASP 参数，15ms）在线程池里算；用户不存在时也算一次假哈希，响应时间不泄露用户名是否存在（测试确认那个假哈希的明文不能登录任何人）。

## 2. RLS：第二道防线
**价值**（实验）：漏写过滤条件时，只靠应用层 5 万个知识库全部泄露；有 RLS 只看到该用户应当可见的 705 个。compose 实测：api 角色下以 bob 的身份 `SELECT count(*) FROM knowledge_bases`，17 个里只看到 1 个。

**策略的组织方式**（迁移 0011）：
- 知识库：读 = 所有者，或部门共享且同部门；写 = 所有者。两条宽松策略之间是“或”。
- 文档、分块、图谱（7 张表）：`EXISTS (SELECT 1 FROM knowledge_bases WHERE id = knowledge_base_id)`——子查询本身也受知识库策略约束，所以“父行看得见，子行才看得见”。
- 对话：所有者；消息、运行记录、对话关联的知识库跟着对话走。
- 产物：所有者。用量：本人；部门负责人看本部门；管理员看全部。
- users / departments / user_sessions 不启用：登录发生在“知道我是谁”之前。

**四个陷阱**（都实测过）：
1. 超级用户和表所有者绕过 RLS——原来的部署就是超级用户，直接加策略完全不生效。所以拆了三个数据库角色。
2. 会话级 `SET` 会在连接池里串到下一个请求——必须用事务级 `set_config(..., true)`。
3. 没设置身份时 `current_setting(..., true)` 返回 NULL，策略结果为假——默认拒绝，漏设置不会泄露，只会“什么都看不到”。
4. 没写 `WITH CHECK` 时 `USING` 也用于检查新行——冒名写入被拒。

**一个测试陷阱**：“连接池不串号”的测试第一版只读，结束时回滚；而回滚会把会话级设置一起撤销，所以把实现改成会话级，测试照样通过。真实请求的写操作会提交，泄漏就会发生。测试改成提交的事务后，变异检查才能发现问题。

## 3. 角色与可见性
| | 成员 | 部门负责人 | 管理员 |
|---|---|---|---|
| 自己的对话、知识库、产物 | ✓ | ✓ | ✓ |
| 同部门共享的知识库 | 只读 | 只读 | 只读（同部门时） |
| 用量 | 自己 | 自己、本部门 | 自己、任意部门、全站 |
| 管理用户和部门 | | | ✓ |

- 共享到“所有者当时所在的部门”（记在知识库上）；没有部门的用户不能共享。
- 对话关联共享库后，执行时还会按可见性重新过滤：被取消共享就不再检索。
- 看得见但不是所有者 → 403（只读）；看不见 → 404（不泄露是否存在）。

## 4. 用量：从源头采集，查询时聚合
- **模型调用**：LangChain 回调挂在图的配置上，`on_llm_end` 时写一行。实验确认节点内部的调用（结构化输出、bind_tools、Send 并行的子 Agent、流式）都能捕获；而且与图是否提交无关——崩溃前已花的钱也记得到（4.3 的局限在这里解决了）。
- **请求**：提交 run 时与用户消息同一事务写一行，不会出现“有请求没记录”。
- **检索、图谱抽取**：各写一行（图谱抽取与进度同一事务）。
- **聚合**：按天 GROUP BY。实验：百万行时个人 / 部门 5~21ms，全站 30 天 261ms；演示规模不需要汇总表。
- **范围**：服务层按角色检查（越权 403），RLS 策略按同样的规则限制。

## 5. 部署与第一个管理员
- migrate 服务：`alembic upgrade head` → 建 checkpoint 表 → `python -m agentnexus.storage.roles`（创建 / 更新角色并授权；每次部署都跑，新表才有授权）。
- `.env` 需要 `DB_API_PASSWORD`、`DB_WORKER_PASSWORD`。
- 第一个管理员（密码从环境变量读，不进 shell 历史）：
  `docker compose exec -e PW='<密码>' api python -m agentnexus.cli create-user --name admin --role admin --password-env PW`
- 升级前就存在的用户没有密码，不能登录：`python -m agentnexus.cli set-password --name <用户名>`。

## 6. 自测题
1. 会话和 JWT 各自怎么做“退出后立即失效”？为什么会话方案要防 CSRF？
2. CSRF 中间件为什么放行没有 Origin 的请求？为什么 Nginx 的 `$host` 会让所有写请求失败？
3. 为什么密码哈希要放线程池？用户不存在时为什么也要算一次哈希？
4. RLS 的价值是什么？为什么原来的部署直接加策略完全不生效？
5. 为什么必须用事务级设置？“连接池不串号”的测试为什么第一版测不出问题？
6. 子表（文档、消息）的策略为什么写成 `EXISTS (SELECT … FROM 父表)`？
7. worker 为什么用 BYPASSRLS 角色？这会削弱隔离吗？
8. 为什么用回调采集用量？请求为什么和 run 写在同一事务？
9. 关联了共享知识库之后被取消共享，会发生什么？

## 7. 已知局限
- ~~没有登录失败次数限制~~：阶段 6.4 已加（Redis 按“账号 + IP”和按 IP 计数，见 decisions/0025 和根目录 `SECURITY.md`）。
- 新建的表要等下一次部署时 `storage/roles.py` 授权。
- 部门负责人在看板上能看到同部门所有人的用户名和用量。
- 用户换部门后，之前共享的知识库仍共享给旧部门，需要所有者重新共享。
