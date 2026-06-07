# RD-Agent vs 当前项目 对比分析与改进建议

> ⚠️ **历史稿(2026-01-29),已 STALE — 勿当现状依据。** 本稿提出并"完成"的 `agents/core/`(experiment/trace/scenario/evolving_kb 等 RD-Agent CoSTEER 实现)**已于 2026-06-06 `b89b732` 整簇删除**(只剩 `__pycache__`;`AttributionType` 迁到 `agents/attribution_types.py`)。生产架构现为**四池(HG/S/E)解耦流水线**。当前状态以 `CLAUDE.md` + `docs/DEVELOPMENT_PLAN.md` §2A 为准;本稿仅作 RD-Agent 方法学对比的历史参考。

## 🎯 执行摘要

通过深入分析RD-Agent的实现，识别了6个主要改进领域。**现已全部完成核心实现**。

| 优先级 | 改进领域 | 状态 | 实现文件 |
|-------|---------|------|---------|
| **[P0]** | 实验追踪系统 | ✅ 完成 | `experiment.py`, `trace.py` |
| **[P0]** | 知识库进化 | ✅ 完成 | `knowledge.py`, `evolving_rag.py` |
| **[P1]** | 反馈循环增强 | ✅ 完成 | `feedback.py`, `pipeline.py` |
| **[P1]** | Trace DAG结构 | ✅ 完成 | `trace.py` |
| **[P1]** | 场景抽象 | ✅ 完成 | `scenario.py` |
| **[P2]** | 组件解耦 | ✅ 完成 | `pipeline.py` |

---

## 1. 实验追踪系统 [P0]

### RD-Agent实现

```python
# rdagent/core/experiment.py
class Experiment:
    def __init__(self, sub_tasks, hypothesis=None):
        self.hypothesis = hypothesis
        self.sub_tasks = sub_tasks
        self.sub_workspace_list = [None] * len(sub_tasks)
        self.running_info = RunningInfo()  # result, running_time
        
# rdagent/core/evolving_framework.py        
@dataclass
class EvoStep:
    evolvable_subjects: EvolvableSubjects
    queried_knowledge: QueriedKnowledge | None = None
    feedback: Feedback | None = None
```

### 当前项目问题

- `AlphaCandidate` 只是数据容器，没有运行时信息
- 没有 `EvoStep` 概念将假设/知识/反馈关联
- 难以追踪"这个alpha是基于什么假设、什么知识生成的"

### 改进建议

```python
# 新增: backend/agents/core/experiment.py
@dataclass
class AlphaExperiment:
    """单次实验的完整记录"""
    hypothesis: Hypothesis
    expression: str
    queried_knowledge: List[str]  # 生成时查询的知识
    
    # 执行信息
    simulation_result: Optional[Dict] = None
    running_time_ms: Optional[int] = None
    
    # 反馈
    feedback: Optional[ExperimentFeedback] = None
    
    # 衍生
    derived_from: Optional[str] = None  # 父实验ID
    optimization_of: Optional[str] = None  # 优化目标

@dataclass
class EvoStep:
    """进化步骤 - 关联假设、知识和反馈"""
    experiment: AlphaExperiment
    queried_knowledge: QueriedKnowledge
    feedback: ExperimentFeedback
    
    # 进化轨迹
    parent_step: Optional['EvoStep'] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
```

---

## 2. 知识库进化 [P0]

### RD-Agent实现

```python
# rdagent/core/evolving_framework.py
class RAGStrategy:
    def query(self, evo, evolving_trace) -> QueriedKnowledge:
        """基于进化轨迹查询相关知识"""
        
    def generate_knowledge(self, evolving_trace, return_knowledge=False) -> Knowledge:
        """从进化轨迹生成新知识"""
        
    def dump_knowledge_base(self):
        """持久化知识库"""
```

### 当前项目问题

- `RAGService` 只能存储/查询模式，不能从实验生成知识
- 没有 `generate_knowledge` 从成功/失败中提取规则
- 知识没有版本化或进化

### 改进建议

```python
# 增强: backend/agents/services/rag_service.py

class EvolvingRAGService(RAGService):
    """支持知识生成的RAG服务"""
    
    async def generate_knowledge_from_trace(
        self,
        evolving_trace: List[EvoStep],
        llm_service: LLMService
    ) -> List[KnowledgeEntry]:
        """
        从进化轨迹生成知识
        
        规则提取:
        1. 成功实验 -> "If [context], then [approach] works"
        2. 失败实验 -> "If [context], avoid [approach]"
        3. 优化成功 -> "If [initial metrics], try [modification]"
        """
        
    async def query_for_experiment(
        self,
        hypothesis: Hypothesis,
        dataset_id: str,
        previous_experiments: List[AlphaExperiment]
    ) -> QueriedKnowledge:
        """
        为新实验查询相关知识
        
        考虑:
        1. 相似假设的历史结果
        2. 相同数据集的成功模式
        3. 类似失败的避免策略
        """
```

---

## 3. 反馈循环增强 [P1]

### RD-Agent实现

```python
# rdagent/core/proposal.py
class HypothesisFeedback(ExperimentFeedback):
    def __init__(self,
        observations: str,           # 观察到的现象
        hypothesis_evaluation: str,  # 假设是否被验证
        new_hypothesis: str,         # 建议的新假设
        decision: bool,              # 是否成功
        ...
    ):
```

### 当前项目问题

- `FeedbackAgent.learn_from_round` 缺少结构化输出
- 没有生成 `new_hypothesis`
- 没有明确的 `hypothesis_evaluation`

### 改进建议

```python
# 新增: backend/agents/core/feedback.py

@dataclass
class HypothesisFeedback:
    """结构化假设反馈"""
    
    # 观察
    observations: str  # 实验观察到的现象
    
    # 评估
    hypothesis_supported: bool  # 假设是否被支持
    hypothesis_evaluation: str  # 详细评估
    attribution: str  # "hypothesis" | "implementation" | "both"
    
    # 决策
    decision: bool  # 是否成功
    should_continue_direction: bool  # 是否继续这个方向
    
    # 建议
    new_hypothesis: Optional[str]  # 建议的新假设
    new_hypothesis_rationale: str
    
    # 知识
    knowledge_extracted: List[str]  # "If..., then..." 规则
    confidence: float  # 知识置信度

class Experiment2Feedback:
    """从实验生成反馈"""
    
    async def generate_feedback(
        self,
        experiment: AlphaExperiment,
        trace: List[EvoStep]
    ) -> HypothesisFeedback:
        """
        生成结构化反馈
        
        包括:
        1. 假设是否被验证 (考虑alignment)
        2. 建议的下一步假设
        3. 提取的知识规则
        """
```

---

## 4. Trace DAG结构 [P1]

### RD-Agent实现

```python
# rdagent/core/proposal.py
class Trace:
    def __init__(self, scen, knowledge_base=None):
        self.hist: List[NodeType] = []  # (Experiment, Feedback)
        self.dag_parent: List[Tuple[int, ...]] = []  # DAG结构
        
    def get_parents(self, child_idx: int) -> List[int]:
        """获取祖先链"""
        
    def get_sota_hypothesis_and_experiment(self):
        """获取最佳实验"""
```

### 当前项目问题

- `trace_steps` 是线性列表
- 不支持分支探索 (同时测试多个方向)
- 无法追踪"这个实验派生自哪个实验"

### 改进建议

```python
# 新增: backend/agents/core/trace.py

class ExperimentTrace:
    """支持DAG结构的实验追踪"""
    
    NodeType = Tuple[AlphaExperiment, HypothesisFeedback]
    
    def __init__(self, scenario: ScenarioContext):
        self.scenario = scenario
        self.hist: List[self.NodeType] = []
        self.dag_parent: List[Tuple[int, ...]] = []
        self.knowledge_base: EvolvingKnowledgeBase = None
        
    def add_experiment(
        self,
        experiment: AlphaExperiment,
        feedback: HypothesisFeedback,
        parent_idx: Optional[int] = None  # None = new tree
    ):
        """添加实验到DAG"""
        self.hist.append((experiment, feedback))
        
        if parent_idx is None:
            self.dag_parent.append(())  # New root
        else:
            self.dag_parent.append((parent_idx,))
            
    def get_branch_from(self, idx: int) -> List[NodeType]:
        """获取从某点开始的分支"""
        
    def get_sota(self) -> Optional[NodeType]:
        """获取最佳实验"""
        for exp, fb in reversed(self.hist):
            if fb.decision:
                return (exp, fb)
        return None
        
    def get_failed_experiments_for_hypothesis(
        self, 
        hypothesis_text: str
    ) -> List[NodeType]:
        """获取某假设的所有失败实验 (用于判断是否放弃)"""
```

---

## 5. 组件解耦 [P2]

### RD-Agent实现

清晰的职责分离:

```
HypothesisGen -> Hypothesis
     |
     v
Hypothesis2Experiment -> Experiment
     |
     v
Developer -> Experiment (with implementation)
     |
     v
Runner -> Experiment (with result)
     |
     v
Experiment2Feedback -> Feedback
```

### 当前项目问题

- `node_hypothesis` 和 `node_code_gen` 没有明确的转换接口
- 反馈生成散落在多个地方
- 难以替换单个组件

### 改进建议

```python
# 新增: backend/agents/core/pipeline.py

class HypothesisGen(Protocol):
    """假设生成器接口"""
    async def gen(self, trace: ExperimentTrace) -> Hypothesis: ...

class Hypothesis2Experiment(Protocol):
    """假设到实验转换器接口"""
    async def convert(
        self, 
        hypothesis: Hypothesis, 
        trace: ExperimentTrace
    ) -> AlphaExperiment: ...

class ExperimentRunner(Protocol):
    """实验运行器接口"""
    async def run(self, experiment: AlphaExperiment) -> AlphaExperiment: ...

class Experiment2Feedback(Protocol):
    """反馈生成器接口"""
    async def generate(
        self,
        experiment: AlphaExperiment,
        trace: ExperimentTrace
    ) -> HypothesisFeedback: ...

# 工作流组合
class AlphaMiningPipeline:
    def __init__(
        self,
        hypothesis_gen: HypothesisGen,
        h2e: Hypothesis2Experiment,
        runner: ExperimentRunner,
        e2f: Experiment2Feedback
    ):
        self.hypothesis_gen = hypothesis_gen
        self.h2e = h2e
        self.runner = runner
        self.e2f = e2f
        
    async def run_iteration(self, trace: ExperimentTrace):
        # Step 1: Generate hypothesis
        hypothesis = await self.hypothesis_gen.gen(trace)
        
        # Step 2: Convert to experiment
        experiment = await self.h2e.convert(hypothesis, trace)
        
        # Step 3: Run experiment
        experiment = await self.runner.run(experiment)
        
        # Step 4: Generate feedback
        feedback = await self.e2f.generate(experiment, trace)
        
        # Step 5: Update trace
        trace.add_experiment(experiment, feedback)
        
        return experiment, feedback
```

---

## 6. Workspace概念 [P2]

### RD-Agent实现

```python
# rdagent/core/experiment.py
class FBWorkspace:
    def __init__(self):
        self.file_dict: Dict[str, str] = {}  # 代码文件
        self.workspace_path: Path = ...
        
    def inject_files(self, **files): ...
    def execute(self, env, entry): ...
    def create_ws_ckp(self): ...  # 检查点
    def recover_ws_ckp(self): ...  # 恢复
```

### 当前项目适用性

对于BRAIN alpha mining，Workspace概念需要适配:
- 没有文件系统执行
- "代码"是表达式字符串
- 但检查点/恢复对于长时间任务有价值

### 改进建议

```python
# 新增: backend/agents/core/workspace.py

@dataclass
class AlphaWorkspace:
    """Alpha挖掘工作区"""
    
    # 当前状态
    active_expressions: List[str]
    active_hypotheses: List[Hypothesis]
    
    # 检查点
    checkpoint_data: Optional[bytes] = None
    
    def create_checkpoint(self) -> bytes:
        """创建检查点供恢复"""
        import pickle
        return pickle.dumps({
            "expressions": self.active_expressions,
            "hypotheses": [h.__dict__ for h in self.active_hypotheses]
        })
        
    def restore_checkpoint(self, data: bytes):
        """从检查点恢复"""
        import pickle
        state = pickle.loads(data)
        self.active_expressions = state["expressions"]
        # ... restore hypotheses
```

---

## 实施路线图

### Phase 1: 核心数据结构 (1-2天)

1. 创建 `backend/agents/core/` 目录
2. 实现 `AlphaExperiment`, `EvoStep`, `HypothesisFeedback` 数据类
3. 实现 `ExperimentTrace` 支持DAG

### Phase 2: 知识进化 (2-3天)

1. 扩展 `RAGService` 为 `EvolvingRAGService`
2. 实现 `generate_knowledge_from_trace`
3. 添加知识持久化

### Phase 3: 反馈增强 (1-2天)

1. 实现 `Experiment2Feedback` 组件
2. 集成到 `node_evaluate` 节点
3. 更新提示词支持结构化反馈

### Phase 4: 组件解耦 (2-3天)

1. 定义 Protocol 接口
2. 重构现有节点实现接口
3. 创建 `AlphaMiningPipeline` 组合器

---

## 快速验证检查清单

完成后应该能够:

- [ ] 追踪 "这个alpha是基于什么假设生成的"
- [ ] 追踪 "生成时查询了什么知识"
- [ ] 从成功/失败中自动生成 "If..., then..." 规则
- [ ] 判断 "这个假设已经失败3次了，应该放弃"
- [ ] 支持分支探索 "同时测试A和B两个方向"
- [ ] 结构化反馈包含 "新假设建议"
