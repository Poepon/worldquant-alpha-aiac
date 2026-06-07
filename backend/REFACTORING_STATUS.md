# 重构状态报告

> **状态(2026-06-04 复核)**:本文档描述的 2026-01 分层重构(Router→Service→Repository)**已全部完成**——下方原「⚠️ 需要改动」清单中的 Service 创建与大文件拆分均已落地。本文件保留作分层架构的**规约说明**(依赖方向、可测试性标准、改动模板),不再是待办清单。当前系统架构以根目录 `CLAUDE.md` + `docs/DEVELOPMENT_PLAN.md` §2A 为准。⚠️ **2026-06-06 `b89b732` 大改后**:FLAT/ONESHOT 调度、`agents/core/`、`tasks/orchestrator.py` **已退役删除**;生产架构 = **四池(HG/S/E)解耦流水线**(`backend/pool/`)。`agents/pipeline/` 仍在(被池复用)、`services/optimization/` 仍在(loop OFF)。

## 一、代码状态分类

### ✅ 已完成重构 - 不需要改动

这些模块已经按照新架构重构完成，具有良好的模块化、可测试性：

| 模块 | 路径 | 说明 |
|------|------|------|
| **协议层** | `backend/protocols/` | 定义了 BrainProtocol, LLMProtocol, RepositoryProtocol |
| **模型层** | `backend/models/` | 拆分为 alpha.py, task.py, knowledge.py 等模块 |
| **Repository层** | `backend/repositories/` | AlphaRepository, TaskRepository, KnowledgeRepository |
| **BaseService** | `backend/services/base.py` | 通用 CRUD 和事务管理 |
| **AlphaService** | `backend/services/alpha_service.py` | Alpha 业务逻辑 |
| **DashboardService** | `backend/services/dashboard_service.py` | 仪表盘统计 |
| **TaskService** | `backend/services/task_service.py` | 任务管理业务逻辑 |
| **适配器层** | `backend/adapters/` | BrainAdapter 实现 BrainProtocol |
| **LLM服务** | `backend/agents/services/llm_service.py` | 统一的 LLM 调用接口 |
| **任务模块** | `backend/tasks/` | 拆分为 mining_tasks, feedback_tasks, sync_tasks |
| **测试基础设施** | `backend/tests/fixtures/` | MockBrainAdapter, MockLLMService |
| **Alphas Router** | `backend/routers/alphas.py` | 已通过 Service 层访问 |
| **Dashboard Router** | `backend/routers/dashboard.py` | 已通过 Service 层访问 |
| **Tasks Router** | `backend/routers/tasks.py` | 已通过 TaskService 访问 |

---

### ✅ 已完成 - Service 层全部创建

下列 router 对应的 Service 均已创建(`task/run/dataset/operator/knowledge/config_service.py` 全部存在):

| 文件 | 对应 Service | 状态 |
|------|----------|------|
| `routers/tasks.py` | `TaskService` | ✅ |
| `routers/runs.py` | `RunService` | ✅ |
| `routers/datasets.py` | `DatasetService` | ✅ |
| `routers/operators.py` | `OperatorService` | ✅ |
| `routers/knowledge.py` | `KnowledgeService` | ✅ |
| `routers/config.py` | `ConfigService` | ✅ |

> 注:个别 router 历史上仍残留 inline DB 查询。改动这些文件时**优先走对应 Service,勿重新引入 inline query**(同 `CLAUDE.md` 分层规则)。

---

### ✅ 已完成 - 大文件拆分

| 原文件 | 拆分结果 | 状态 |
|------|------|------|
| `agents/graph/nodes.py` | `nodes/{base,generation,validation,evaluation,persistence}.py` | ✅ |
| `agents/prompts.py` | `prompts/` 包(`generation/hypothesis/validation/analysis/...`,旧 `prompts.py` 为 re-export shim) | ✅ |

---

### ✅ 不需要改动 - 独立工具模块

这些模块相对独立，可测试性良好：

| 模块 | 说明 |
|------|------|
| `alpha_scoring.py` | 纯函数，可独立测试 |
| `alpha_semantic_validator.py` | 纯函数，可独立测试 |
| `genetic_optimizer.py` | 独立算法模块 |
| `diversity_tracker.py` | 独立跟踪器 |
| `selection_strategy.py` | 独立策略模块 |

---

## 二、模块化原则

### 2.1 目录结构规范

```
backend/
├── protocols/          # 接口定义层 - 只有抽象接口
│   ├── brain_protocol.py
│   ├── llm_protocol.py
│   └── repository_protocol.py
│
├── models/             # 数据模型层 - 只有 SQLAlchemy 模型
│   ├── alpha.py
│   ├── task.py
│   └── ...
│
├── repositories/       # 数据访问层 - 只有数据库操作
│   ├── base_repository.py
│   ├── alpha_repository.py
│   └── ...
│
├── services/           # 业务逻辑层 - 编排 Repository 和外部服务
│   ├── base.py
│   ├── alpha_service.py
│   └── ...
│
├── adapters/           # 外部服务适配层 - 实现 Protocol
│   └── brain_adapter.py
│
├── routers/            # API 层 - 只调用 Service，不直接访问 DB
│   ├── alphas.py
│   └── ...
│
├── agents/             # Agent 层 - 核心业务编排
│   ├── mining_agent.py
│   └── ...
│
├── tasks/              # Celery 任务层 - 只调用 Service
│   ├── mining_tasks.py
│   └── ...
│
└── tests/              # 测试层
    ├── unit/           # 单元测试
    ├── integration/    # 集成测试
    └── fixtures/       # Mock 实现
```

### 2.2 依赖规则

```
Router → Service → Repository → Database
                 → Protocol(Adapter)
                 → Protocol(LLM)
```

**禁止的依赖:**
- ❌ Router 直接访问 Database
- ❌ Router 直接调用 Adapter
- ❌ Service 直接创建外部客户端 (应通过 Protocol 注入)

---

## 三、可测试性标准

### 3.1 每个模块必须满足

1. **依赖注入**: 所有外部依赖通过构造函数注入
2. **协议依赖**: 依赖抽象接口而非具体实现
3. **纯函数优先**: 工具方法尽量写成纯函数
4. **单一职责**: 每个类/函数只做一件事

### 3.2 测试覆盖要求

| 层级 | 测试类型 | 覆盖率目标 |
|------|----------|------------|
| Repository | 单元测试 | 90%+ |
| Service | 单元测试 + 集成测试 | 85%+ |
| Router | 集成测试 | 80%+ |
| Agent | 单元测试 (Mock 外部依赖) | 75%+ |

### 3.3 Mock 使用规范

```python
# ✅ 正确: 通过依赖注入使用 Mock
def test_mining_service(db_session, mock_brain_adapter):
    service = MiningService(db_session, brain=mock_brain_adapter)
    # ...

# ❌ 错误: 依赖全局单例
def test_bad_example():
    service = MiningService(db_session)  # 内部使用全局 brain_adapter
```

---

## 四、可评估性标准

### 4.1 模块级评估

每个模块应该可以独立评估：

```python
# 示例: 评估 AlphaRepository
async def evaluate_alpha_repository():
    async with test_session() as db:
        repo = AlphaRepository(db)
        
        # 创建性能
        start = time.time()
        for i in range(100):
            await repo.create(sample_alpha())
        create_time = time.time() - start
        
        # 查询性能
        start = time.time()
        for i in range(100):
            await repo.get_by_task_id(task_id)
        query_time = time.time() - start
        
        return {
            "create_ops_per_sec": 100 / create_time,
            "query_ops_per_sec": 100 / query_time,
        }
```

### 4.2 回归测试基线

`backend/tests/baseline.json` 存储性能基线：

```json
{
  "alpha_repository": {
    "create_ops_per_sec": 500,
    "query_ops_per_sec": 1000
  },
  "mining_service": {
    "avg_alpha_gen_time_ms": 2000,
    "success_rate": 0.7
  }
}
```

### 4.3 评估检查点

- [ ] 每个 Repository 有独立性能测试
- [ ] 每个 Service 有端到端测试
- [ ] Agent 有模拟运行测试 (使用 Mock)
- [ ] 每次提交运行回归测试

---

## 五、下一步改动清单

> 原 P0/P1 清单(创建剩余 Service、修复 Router、拆分 `nodes.py`/`prompts.py`)**已全部完成**(见上文 ✅ 段落)。本文档现仅作分层规约参考,无遗留待办。新工作的现状以 `CLAUDE.md` 与 `docs/INDEX.md` 为准。

---

## 六、验证检查列表

在每次改动后验证：

```bash
# 1. 类型检查
python -m mypy backend/

# 2. 单元测试
pytest backend/tests/unit/ -v

# 3. 集成测试
pytest backend/tests/integration/ -v

# 4. 导入检查 (确保模块可以正确导入)
python -c "from backend.services import AlphaService, DashboardService"
python -c "from backend.repositories import AlphaRepository, TaskRepository"
python -c "from backend.protocols import BrainProtocol, LLMProtocol"
```
