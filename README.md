# 边缘遥测 LSM 合并工坊

北岭边缘计算小组的有序键值写入与合并演练：记录写入批次、配置层级策略、后台执行刷盘与合并，用最后写入字典 Oracle 核查读结果，防止墓碑误删导致旧值复活；另设乐观事务演练场，在同一批遥测键上交错两笔事务，验证快照读、读谓词冲突与整笔退回。

**这是 Python 确定性教学 LSM/并发模型。MySQL 8.0 仅保存业务元数据，不是对 MySQL/InnoDB 存储内核或 MVCC 事务实现的复刻或性能测试。** 页面与导出结果均保留此边界。

## 场景与模块

北岭边缘计算小组有 7 人，负责低内存遥测站点的存储方案验证。过去在电子表格记录写入序列、手工画 SSTable、用本地脚本核对合并结果，三个痛点是配置与操作序列混在一起、删除键的历史难复查、读写放大缺少一致口径。上游为站点模拟写入序列；本站替代输入归档、配置组合与可重现演练；下游是边缘存储评审人与值班交接。

- 写入批次：有序 put/delete，支持重复键、墓碑、完整操作预览、输入校验。
- 层级配置：memtable 不同键容量、SSTable 合并触发数、层级上限、最终完整合并开关。
- 合并队列：创建试验时持久化输入及配置快照，Redis 分发，独立 worker 执行并记录状态。
- 读放大证据：显示合并前后层级、读写放大、真实存活键、Oracle 摘要、刷盘/合并轨迹、JSON 导出。
- 事务演练（05 页面）：从写入批次建立带逻辑版本的已提交键空间，交错操作两笔事务，演练快照读、读己写、谓词冲突整笔退回、不相交事务并发提交、幂等重试与物理合并。
- 管理员审计：记录写操作账号、路由与 HTTP 结果，包含权限拒绝。

| 角色 | 页面与权限 | 典型动作 |
|---|---|---|
| admin 管理员 | 全业务页面与审计 | 登记批次/配置、执行、导出、事务演练、查看审计 |
| operator 操作员 | 五业务页面，不可看管理员审计 | 新建输入与配置、提交 worker、检查与下载结果、开事务演练 |
| viewer 观察员 | 五业务页面只读与导出 | 阅读层级和 Oracle 证据，复核事务谓词与冲突键，写请求在 API 层403 |

## 启动与账号

```bash
cp .env.example .env
docker compose config --quiet
docker compose up -d --build
# 默认启动自动执行；手动重跑不覆盖已存在的输入、配置、账号
docker compose exec -T backend python -m scripts.seed_demo
```

[前端入口](http://localhost:18245) · [后端接口文档](http://localhost:19245/docs)，数据库映射端口17745。

三个本地演示账号是 `admin@beiling.test` / `operator@beiling.test` / `viewer@beiling.test`，统一密码 `EdgeStore!17`。`.env.example` 提供固定本地数据库与JWT演示凭据，上线前替换。

```bash
# 只清理本项目与其演示卷
docker compose down -v --remove-orphans
```

## 09:00—10:00 的工作剧本

1. 09:00 operator 登录，打开“北岭站点-早班写入”：12条操作，8个不同键。
2. 09:05 查看 sensor-01 的18.2→18.8→19.0覆盖记录，sensor-02和sensor-03有删除墓碑。
3. 09:10 新建 `shift-20261117` 输入，或复用“旧值复活回归集”。
4. 09:15 选择“小内存-完整合并”：memtable键容量2，表触发数2，3层，最后完整合并。
5. 09:20 创建试验并提交worker，在队列刷新至“Oracle一致”。
6. 09:25 打开读放大证据，确认早班输入最终6个存活键，sensor-02/03未复活。
7. 09:30 下载 `lsm-trial-<id>.json`，包含操作/配置快照、层级、轨迹与校验和。
8. 09:40 打开05“事务演练”：两笔事务交错读 `sensor-01…sensor-05`，一笔先提交后另一笔整笔退回，页面列出冲突键；再对不相交范围各开一笔验证并发提交成功，最后执行物理合并确认在途事务不受影响。
9. 09:50 viewer复核，创建试验被403拒绝；10:00 admin检查操作审计。

Seed另含“旧值复活回归集”和“全部删除边界集”；配置另含“保留层级观测”和“宽缓冲-完整合并”。账户、批次、配置以自然键查缺插入，重复seed不改变已存在字段。seed还会建立“两班巡检-事务演练场”（引用早班写入批次），已存在则跳过。

## 核心算法与指标口径

输入为有序操作数组和参数配置：

- 操作数1–2000；键长1–64；put的value必须是1个不超过200字符的字符串（允许空字符串）；delete的value忽略。
- memtable_limit范围2–64，按不同键数量计容量；l0_trigger范围2–8；max_levels范围2–6。
- 每个用户操作分配单调序号。memtable同键覆盖；达到容量后按键排序刷成L0 SSTable。末尾不足容量也刷盘。
- 某层表数达到触发值时，合并该层所有表到下一层；相同键按最高序号保留记录。最深层在原层合并。**层级合并始终保留墓碑**，不会提早清理。
- 若打开最终完整合并，收集全部层级所有表，按序号合并完整历史后才移除墓碑，存放在最深层。不开启时保留中间层级与墓碑。
- 点查遍历范围覆盖键的SSTable，在候选记录中选择最高序号；带墓碑则不返回。固定探测集合为全部输入键加一个不存在的探针键。没有Bloom过滤器模型。
- 读放大 = 固定点查集合的范围命中SSTable读取总数 / 探测键数；合并前后使用相同集合。写放大 = flush/各次compaction真实输出条目总数 / 用户操作数，**不是物理字节或磁盘I/O**。
- Python另从原始操作顺序构造最后写入字典Oracle；合并前、合并后均必须与Oracle一致，否则试验failed。结果与Oracle各自对有序JSON求SHA-256；同输入/配置结果确定，无随机时钟项。

输出包括真实计算的层级表数/键范围/墓碑数、每步刷盘合并轨迹、输入键数、存活键、写出条目、读写放大、Oracle及实际键值、摘要。空输入和超限在提交时422，引用不存在404，重名409，排队/运行总数超过20时429；worker失败保存error和failed状态。

## 事务演练语义（乐观并发）

演练场从一个写入批次按操作顺序建立已提交键空间，每个逻辑键持有单调递增的 `version`，delete 写入墓碑。状态全部保存在 `drill_sessions.state` JSON 中，每次状态迁移整份落库；服务端按演练场加锁串行化，后端重启后在途事务、读谓词与暂存区完整恢复。

- **开始即快照**：`begin` 固定当前已提交键空间与 `commit_seq`。事务内的范围读始终按开始时的已提交状态返回，即使其他事务随后提交了新值；范围内再叠加本事务自己暂存的 put/delete（读己写），暂存删键不返回。
- **读谓词**：每次范围读登记 `[lo, hi]`。提交校验时拿当前已提交键空间与范围求交——范围内任何键的版本与读时不一致即冲突，因此范围内的**新插入键、删键、改值**都能捕获，而不只是读过的旧键。
- **待写键版本**：每个暂存键记录首次触及时的已提交基线版本（不存在记为 0），提交时版本不符即冲突。
- **整笔退回**：任一谓词或待写键冲突，事务整体 `rolled_back`，暂存的覆盖与删键全部不生效；冲突键精确列出。冲突判定基于逻辑版本而非值，**即使值被改回原样也退回**。
- **不相交可并发**：读范围与待写键互不相交的事务先后提交均成功，互不误伤。
- **幂等提交**：终态事务再次提交只返回保存的结果，不追加版本、不重复生效；人工回滚结果同样可重放。
- **合并不是改写**：物理合并把多个段压成一段并保留墓碑，但不改动任何逻辑版本与 `commit_seq`，在途事务的谓词不会因此失效。

接口前缀 `/drill`：`GET/POST /sessions`、`GET /sessions/{id}`，以及 `begin` / `read` / `write` / `commit` / `rollback` / `compact` 六个动作。viewer 全部只读，写请求在 API 层 403；重名 409、引用不存在 404、键范围或键值非法 422、终态事务再操作 409、未知事务 404。前端 05 页面可在一个屏幕内交错操作两笔进行中的事务，提交后显示成功/退回横幅与冲突键徽章，并保留完整事件轨迹与物理段版本证据。

## 技术栈与代码组织

| 服务 | 实现与作用 |
|---|---|
| frontend | 原生HTML/CSS/JS ES模块，Nginx1.27，土橙材料样本册视觉 |
| backend | Python3.11、FastAPI、SQLAlchemy、PyMySQL、JWT与PBKDF2密码 |
| worker | 独立Python进程，Redis BLPOP，LSM演算，数据库恢复队列，心跳健康检查 |
| db | MySQL8.0，持久化3个业务实体、用户与审计 |
| redis | Redis7，异步队列与持久卷 |

`backend/src/`分为config/models/schemas/services/controllers/routes/middlewares/utils/constants；`backend/scripts/seed_demo.py`种子与初始化；`backend/migrations/README.md`为schema v1/v2说明。3实体dataset/config/trial各自贯穿model/schema/service/route、前端api/store/page；事务演练场drill_sessions为第4个持久化实体，纯函数引擎在`services/drill_engine.py`，锁与持久化在`services/drill.py`，前端为`pages/drill.js`单页双事务工作台。试验结果保存在Trial JSON字段中，详情页为独立业务页面。

`frontend/src/`包含api/stores/types/components/common/hooks/pages/router/utils/constants，表格/状态/页头跨页共享，HTML转义与反馈工具复用。角色/任务状态/操作类型/事务状态枚举在`backend/src/constants/enums.py`和`frontend/src/constants/enums.js`。模型默认任务状态使用后端枚举，前端状态组件使用前端枚举。后端测试在`backend/tests/`：`test_drill_engine.py`为纯引擎25项语义检查，`test_drill_service.py`为SQLite服务集成检查（需安装requirements），`test_drill_service_stub.py`为零依赖标准库桩件版服务检查。

Docker恰好5服务，全部healthcheck；backend依赖db/redis healthy，worker还等待backend完成seed；frontend等待backend healthy。命名卷保存MySQL、Redis及worker证据。Nginx `/api/`剥前缀转发后端。

环境变量：COMPOSE_PROJECT_NAME、FRONTEND_PORT、BACKEND_PORT、DB_PORT、MYSQL_PASSWORD、MYSQL_ROOT_PASSWORD、JWT_SECRET、SEED_DEMO；容器连接还配置DATABASE_URL/REDIS_URL。端口可在.env整体调整避免冲突。

本地开发推荐修改代码后`docker compose up -d --build`。也可用虚拟环境安装backend/requirements.txt，启动Compose db/redis，设置可达DATABASE_URL/REDIS_URL/JWT_SECRET，进入backend执行`uvicorn src.main:app --reload`；worker须有可写`/artifacts`并执行`python -m src.worker`。

## 已知边界

- 存储过程为内存教学模型，忽略磁盘块、压缩、Bloom过滤器、WAL持久化、并行compaction与真实故障恢复。MySQL负责保存网页业务输入和输出。
- 前端创建独立批次/配置，不提供原记录编辑删除；新版本新建记录，试验总是固定输入快照。
- 最终全历史合并允许去掉墓碑；没有模拟跨副本历史/快照读，因此不能将该规则直接用于生产数据库。
- 单worker与按钮刷新；不提供多worker分布式锁、SSE。
- 事务演练是单进程教学乐观并发：会话锁串行化状态迁移，不模拟死锁、锁等待、隔离级别切换或跨节点一致性；谓词校验是教学版范围交集，不对应任何真实数据库的序列化实现。
- Web双字体来自Google Fonts，离线回退到serif/monospace。

MIT License，见LICENSE。实际测试与截图位于批次的`test-artifacts/gb-4106/`，源码内不保存测试证据。
