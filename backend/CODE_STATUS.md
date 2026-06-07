# 代码状态和改动指南

> 此文档明确标注每个模块的状态，指导渐进式重构。
> 
> **最后更新**: 2026-01-29 - 所有重构任务已完成
>
> **覆盖范围(2026-06-04 复核)**:本文档准确描述 2026-01 完成的**分层重构骨架**(protocols/models/repositories/services/adapters/routers/tasks),这部分今天仍成立。但它**不覆盖** 2026-01 之后构建的主线,包括:`agents/core/`(RD-Agent CoSTEER)、`agents/pipeline/`(FLAT 生产者-消费者流水线)、`services/optimization/`(优化闭环)、`tasks/orchestrator.py`、LLM 路由(`agents/services/llm_service.py:resolve_model_for`)、FLAT/ONESHOT 调度模型,以及 tier/cascade 系统的退役。这些以根目录 `CLAUDE.md` 为准;历史文档见 `docs/archive/`。

---

## ⚠️ 知识库检索分层 L0/L2/L3 known-dead(2026-06-07 实测)

`agents/hierarchical_rag.py` 的 4 层检索**只有 L1(pillar/dataset-category)真转**;**L0(exact pattern_hash)/ L2(family_signature)/ L3(field 级)结构性永久 dormant**——三层都 gate 在 `if not current_expression: return`(:411/:487/:947),而 HG `generation.py:116` 在生成表达式**之前**调 RAG、不传 `current_expression`,self-correct(`validation.py`)走独立 Redis correction KB 也不传。**live 铁证**:`r8_query_log` layer_hits L1=8709 / L0=L2=L3=0、`current_expression_hash` 0/2211。
**勿基于"4 层都在转"做检索优化(错前提)。** 复活 L0/L2/L3 = 生成端改造,且实证广度/检索非当前杠杆(execution-limited),止损期不做。详见 `docs/kb_redesign_unified_2026-06-07.md` §3.2 / §6。

---

## 📁 目录总览

```
backend/
├── 🟢 protocols/           # 完成 - 接口定义层
├── 🟢 models/              # 完成 - 数据模型层 (已拆分)
├── 🟢 repositories/        # 完成 - 数据访问层
├── 🟢 services/            # 完成 - 业务逻辑层 (已添加所有 Service)
├── 🟢 adapters/            # 完成 - 外部服务适配层
├── 🟢 routers/             # 完成 - API 层 (全部通过 Service)
├── 🟢 agents/              # 完成 - Agent 层 (nodes, prompts 已拆分)
├── 🟢 tasks/               # 完成 - Celery 任务层 (已拆分)
└── 🟢 tests/               # 完成 - 测试基础设施
```

---

## 🟢 已完成重构的模块

### 基础设施层 (Foundation)

| 文件 | 说明 | 可测试性 |
|------|------|----------|
| `protocols/__init__.py` | 协议导出 | N/A |
| `protocols/brain_protocol.py` | BRAIN API 接口定义 | N/A |
| `protocols/llm_protocol.py` | LLM 服务接口定义 | N/A |
| `protocols/repository_protocol.py` | 数据访问接口定义 | N/A |
| `database.py` | 数据库连接 | ✅ |
| `config.py` | 配置管理 | ✅ |
| `celery_app.py` | Celery 配置 | N/A |

### 数据层 (Data)

| 文件 | 说明 | 可测试性 |
|------|------|----------|
| `models/__init__.py` | 模型导出 | N/A |
| `models/base.py` | 枚举定义 | N/A |
| `models/alpha.py` | Alpha 模型 | N/A |
| `models/task.py` | Task 模型 | N/A |
| `models/knowledge.py` | Knowledge 模型 | N/A |
| `models/metadata.py` | 元数据模型 | N/A |
| `models/config.py` | 配置模型 | N/A |
| `repositories/__init__.py` | Repository 导出 | N/A |
| `repositories/base_repository.py` | 通用 CRUD | ✅ MockDB |
| `repositories/alpha_repository.py` | Alpha 数据访问 | ✅ MockDB |
| `repositories/task_repository.py` | Task 数据访问 | ✅ MockDB |
| `repositories/knowledge_repository.py` | Knowledge 数据访问 | ✅ MockDB |

### 服务层 (Services) - 全部完成

| 文件 | 说明 | 可测试性 |
|------|------|----------|
| `services/base.py` | 基础服务类 | ✅ |
| `services/alpha_service.py` | Alpha 业务逻辑 | ✅ MockRepo |
| `services/dashboard_service.py` | 仪表盘统计 | ✅ MockRepo |
| `services/task_service.py` | 任务管理 | ✅ MockRepo |
| `services/dataset_service.py` | 数据集管理 | ✅ MockRepo |
| `services/knowledge_service.py` | 知识库管理 | ✅ MockRepo |
| `services/run_service.py` | 实验运行管理 | ✅ MockRepo |
| `services/operator_service.py` | 算子管理 | ✅ MockRepo |
| `services/config_service.py` | 配置管理 | ✅ MockRepo |

### 适配器层 (Adapters)

| 文件 | 说明 | 可测试性 |
|------|------|----------|
| `adapters/__init__.py` | 适配器导出 | N/A |
| `adapters/brain_adapter.py` | BRAIN API 实现 | ✅ MockBrain |

### API 层 (Routers) - 全部完成

| 文件 | Service | 可测试性 |
|------|---------|----------|
| `routers/alphas.py` | AlphaService | ✅ MockService |
| `routers/dashboard.py` | DashboardService | ✅ MockService |
| `routers/tasks.py` | TaskService | ✅ MockService |
| `routers/datasets.py` | DatasetService | ✅ MockService |
| `routers/knowledge.py` | KnowledgeService | ✅ MockService |
| `routers/runs.py` | RunService | ✅ MockService |
| `routers/operators.py` | OperatorService | ✅ MockService |
| `routers/config.py` | ConfigService | ✅ MockService |

### Agent 层 - 已拆分

| 模块 | 说明 | 文件数 |
|------|------|--------|
| `agents/graph/nodes/` | 节点函数模块化 | 5 |
| `agents/prompts/` | 提示模板模块化 | 7 |

**nodes 拆分结构:**
- `nodes/__init__.py` - 统一导出
- `nodes/base.py` - 辅助函数
- `nodes/generation.py` - RAG/假设/代码生成
- `nodes/validation.py` - 验证和自我修正
- `nodes/evaluation.py` - 模拟和评估
- `nodes/persistence.py` - 保存结果

**prompts 拆分结构:**
- `prompts/__init__.py` - 统一导出
- `prompts/base.py` - 数据类和辅助函数
- `prompts/generation.py` - Alpha 生成提示
- `prompts/hypothesis.py` - 假设和蒸馏提示
- `prompts/validation.py` - 验证和优化提示
- `prompts/analysis.py` - 分析提示
- `prompts/legacy.py` - 遗留模板
- `prompts/registry.py` - 提示注册表

### 任务层 (Tasks)

| 文件 | 说明 | 可测试性 |
|------|------|----------|
| `tasks/__init__.py` | 任务导出 | N/A |
| `tasks/mining_tasks.py` | 挖掘任务 | ✅ MockBrain+MockLLM |
| `tasks/feedback_tasks.py` | 反馈任务 | ✅ MockLLM |
| `tasks/sync_tasks.py` | 同步任务 | ✅ MockBrain |

### 测试基础设施

| 文件 | 说明 |
|------|------|
| `tests/conftest.py` | pytest 配置和 fixtures |
| `tests/fixtures/__init__.py` | Mock 导出 |
| `tests/fixtures/mock_brain.py` | MockBrainAdapter |
| `tests/fixtures/mock_llm.py` | MockLLMService |
| `tests/unit/test_repositories.py` | Repository 单元测试 |
| `tests/unit/test_services.py` | Service 单元测试 |
| `tests/unit/test_new_services.py` | 新 Service 单元测试 |

### 独立工具模块

| 文件 | 说明 | 可测试性 |
|------|------|----------|
| `alpha_scoring.py` | 评分逻辑 | ✅ 纯函数 |
| `alpha_semantic_validator.py` | 语义校验 | ✅ 纯函数 |
| `genetic_optimizer.py` | 遗传算法 | ✅ 纯函数 |
| `diversity_tracker.py` | 多样性追踪 | ✅ 纯函数 |
| `selection_strategy.py` | 选择策略 | ✅ 纯函数 |
| `dataset_selector.py` | 数据集选择 | ✅ 纯函数 |

---

## 🔴 禁止修改的规则

1. **协议文件** (`protocols/`) - 只能添加新方法，不能修改现有签名
2. **模型文件** (`models/`) - 修改需要数据库迁移
3. **测试 fixtures** (`tests/fixtures/`) - Mock 必须与协议保持同步

---

## 改动模板

### 1. 创建新 Service

```python
# services/xxx_service.py
from backend.services.base import BaseService
from backend.repositories import XxxRepository

class XxxService(BaseService):
    def __init__(self, db: AsyncSession):
        super().__init__(db)
        self.xxx_repo = XxxRepository(db)
    
    async def get_xxx(self, id: int):
        return await self.xxx_repo.get_by_id(id)
```

### 2. 重构 Router

```python
# routers/xxx.py (重构前)
@router.get("/{id}")
async def get_xxx(id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Xxx).where(Xxx.id == id))
    return result.scalar_one_or_none()

# routers/xxx.py (重构后)
def get_xxx_service(db: AsyncSession = Depends(get_db)) -> XxxService:
    return XxxService(db)

@router.get("/{id}")
async def get_xxx(id: int, service: XxxService = Depends(get_xxx_service)):
    return await service.get_xxx(id)
```

### 3. 添加单元测试

```python
# tests/unit/test_xxx_service.py
import pytest
from backend.services import XxxService

class TestXxxService:
    @pytest.mark.asyncio
    async def test_get_xxx(self, db_session, sample_xxx):
        service = XxxService(db_session)
        result = await service.get_xxx(sample_xxx.id)
        assert result is not None
```

---

## 验证清单

每次改动后执行：

```bash
# 1. 导入检查
python -c "from backend.services import XxxService"

# 2. 单元测试
pytest backend/tests/unit/test_xxx_service.py -v

# 3. 类型检查 (可选)
python -m mypy backend/services/xxx_service.py
```
