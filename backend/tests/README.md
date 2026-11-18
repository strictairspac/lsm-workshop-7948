# 事务演练测试

本机若没有 Docker / 第三方依赖，可先用零依赖套件验证；完整依赖环境下再跑 SQLite 集成套件。

## backend/tests

| 文件 | 依赖 | 内容 |
|---|---|---|
| `test_drill_engine.py` | 仅标准库 | 纯引擎 25 项：快照读、读己写、谓词冲突（含范围内新增键）、值改回原样仍冲突、不相交并发、幂等提交/回滚重放、合并不改逻辑版本、墓碑保留 |
| `test_drill_service_stub.py` | 仅标准库 | 标准库桩件版服务层 26 项：会话锁串行化、整份状态持久化、模拟后端重启恢复在途事务、404/409/422 错误映射 |
| `test_drill_http_stub.py` | 仅标准库 | 标准库 HTTP 服务 + 真实 `routes/drill.py` 的 19 项端到端检查，含 viewer 403 分支 |
| `test_drill_service.py` | requirements（SQLAlchemy + SQLite） | 与桩件版同构的真实 ORM 集成检查，CI 或装好依赖后运行 |

```bash
# 零依赖（容器外即可）
python3 -I tests/test_drill_engine.py
python3 -I tests/test_drill_service_stub.py
python3 -I tests/test_drill_http_stub.py
# 完整依赖
pip install -r requirements.txt
python3 -I tests/test_drill_service.py
```

## frontend/tests

`drill-render.test.mjs`：Node 内置模块 + 最小 DOM/fetch 桩，验证事务页列表渲染、双事务详情（进行中 + 已退回 + 冲突徽章）、viewer 只读分支。

```bash
node tests/drill-render.test.mjs
```
