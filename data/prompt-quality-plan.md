# Prompt Quality Optimization Plan (W0-W3 续作)

## Context

W0-W3（4 周）完成的工程改进已落地：
- W0 LLM 经济学 reasoning chain → turnover 中位数从历史高位降到 0.16 ✓
- W0.5 self-correlation 三级降级 ✓
- W1 round-level 早停 + EARLY_STOPPED 状态 ✓
- W2 island model 4×12×5 budget-neutral ✓
- W3 KB upsert + HITL 链 + cost-aware bandit ✓ 实测 LIKED → confidence 0.5→0.7→0.9

但实测数据揭示**真实瓶颈不在工程层**：

| 指标 | 实测值 | 含义 |
|---|---|---|
| 全表 avg_sharpe | **0.293** | 生成的因子平均预测力远低于 PASS 门槛 1.5 |
| 全表 avg_fitness | 0.153 | 同上 |
| Task 5 round 1 sharpe 分布 | -0.05 / 0.01 / -0.19 | 4 alpha 全部 FAIL |
| W0 turnover 控制 | 中位数 0.16 ≤ 0.4 | 治本生效 ✓（不是问题） |

**LLM 生成的 alpha 因子本身预测力不足** —— 不是 turnover 风格问题，是 LLM 对"什么因子 work"的判断弱。这超出"调度+反馈循环"优化范畴，需要**生成端模型/prompt 升级**。

## 三条杠杆（按 ROI 排序）

| # | 杠杆 | 实施代价 | 预期收益 |
|---|---|---|---|
| **L1** | **切到 Anthropic Claude API + 显式 prompt caching** | 1 day | LLM 推理能力 ↑（Claude 4.7 Opus > Qwen3-coder-plus）；prompt cache 显式可控（vs Qwen 隐式） |
| **L2** | **持续 few-shot 池**：把 PASS / PASS_PROVISIONAL 的真实 alpha 自动注入 next-round prompt | 2 day | LLM 通过 in-context learning 自我改进；KB 已含 SUCCESS_PATTERN 仅需读取注入 |
| **L3** | **DSPy 自动 prompt 优化**：用 BootstrapFewShot 自动找 reasoning chain 模板的最优 wording | 5-7 day | 移除手工 prompt 调优；指标驱动 |

L1+L2 = 3 day，预期 sharpe 中位数从 0.29 → 0.6+；L3 实验性，可能 sharpe → 0.8+。

## W5：L1 Claude API 切换（3 day）

### 文件改动

| 文件 | 改动 |
|---|---|
| `backend/agents/services/llm_service.py` | 加 `provider="anthropic"` 分支；用 `anthropic.AsyncAnthropic`；system prompt 加 `cache_control={"type": "ephemeral"}` |
| `backend/config.py` | 加 `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` 配置（默认 `claude-haiku-4-5` 跑 generation，`claude-opus-4-7` 跑 hypothesis） |
| `.env.example` / `.env` | 添加 keys |
| `requirements.txt` | `anthropic>=0.40` |
| `agents/services/llm_service.py:call()` | 双 provider 分支：`OPENAI_BASE_URL` 仍 fallback 到 Qwen，但默认走 Anthropic |

### 关键设计

- **Cache budget**：reasoning chain template (~1500 tokens) + 三元组 few-shot (~600 tokens) 放 system prompt 头部
- **Cache hit 验证**：response.usage.cache_read_input_tokens > 80% input tokens
- **Cost tradeoff**：Claude Haiku ~3× Qwen 但 cache 命中后等价

### 验证

- 跑 1 个 task，对比 task 5（Qwen baseline）vs task 6（Claude）的 N=20 alpha avg_sharpe
- 期望 ≥ 0.5（vs Qwen 0.29）

## W6：L2 持续 few-shot 池（2 day）

### 文件改动

| 文件 | 改动 |
|---|---|
| `backend/agents/services/rag_service.py` | 新方法 `get_recent_pass_examples(region, dataset_id, limit=5)`：从 `knowledge_entries WHERE entry_type='SUCCESS_PATTERN' AND created_by IN ('SYSTEM', 'HITL')` 拉最近 N 条+ confidence 排序 |
| `backend/agents/graph/nodes/generation.py:node_code_gen` | prompt 构建前注入 RAG service 拉的 examples，附在 system prompt few-shot 后 |
| `backend/agents/prompts/generation.py` | 三元组 few-shot 模板加入 `{{recent_pass_examples}}` 占位符 |

### 设计要点

- **滚动 5 条**：取最近 1 周 + 最高 confidence 的 5 条，自动滚动
- **去重**：跳过最近 round 已用过的 pattern（防 LLM 反复学同一条）
- **HITL 优先**：created_by='HITL' 的 confidence > 0.7 优先注入（用户偏好信号）

### 验证

- KB 已有 1 条 HITL（id=253，confidence=0.9）+ 240 条 SYSTEM
- 跑 task 验证 LLM 生成包含/借鉴 HITL pattern 的 alpha

## W7-8：L3 DSPy 自动优化（实验性）

按 plan 简化版，不展开。先看 W5+W6 实测数据再决定是否上 L3。

如果 W5+W6 后 avg_sharpe ≥ 0.5：
- L3 可能进一步推 0.5 → 0.7
- 实验性 5-7 day，引入 DSPy 库 + `BootstrapFewShot` 套到现有 reasoning chain 模板

如果 W5+W6 后 avg_sharpe < 0.4：
- 说明 LLM 不是头号瓶颈（可能是 BRAIN 数据集字段限制 / region 限制 / hard gate 阈值）
- 跳过 L3，转向数据维度（多 region 探索 + 替代 dataset）

## 验证 binary success criteria

| 阶段 | 通过条件 | 触发 |
|---|---|---|
| W5 完成 | task 6 avg_sharpe ≥ 0.5（vs Qwen 0.29） | 继续 W6 |
| W6 完成 | task 7 avg_sharpe ≥ 0.7 OR HITL pattern 被 LLM 借鉴 ≥ 1 次 | 继续 W7 |
| W7 完成 | DSPy MIPROv2 跑出 sharpe 提升 ≥ 0.1 | 上线优化后的 prompt |
| 任一阶段不达标 | 触发"数据维度"备选路径（多 region/dataset 探索） | — |

## 不可逆决策（W5 上线前 freeze）

| 决策 | 值 |
|---|---|
| Claude 默认模型 | code_gen=`claude-haiku-4-5`、hypothesis=`claude-opus-4-7` |
| Cache TTL | ephemeral（5 min） |
| Few-shot 池滚动窗口 | 7 天 |
| HITL pattern 优先级 | confidence > 0.7 优先 |

## 风险与回滚

| 风险 | 缓解 |
|---|---|
| Claude API 配额限制 | 双 provider 通过 `LLM_PROVIDER` env 切换；问题时秒切回 Qwen |
| Few-shot 注入污染 prompt | 加 `feature_flag.few_shot_injection` 默认 False，A/B 5 task 后默认 True |
| HITL pattern 数量不足 | W5 之前 KB 只 1 条 HITL；不足时 fallback 到 SYSTEM SUCCESS_PATTERN |
| DSPy 引入依赖 | 实验性周才引入；如果 W5+W6 已达标可跳过 |

## 总周期

- W5 (Claude 切换): 3 day
- W6 (few-shot 池): 2 day
- W7-8 (DSPy 实验): 5-7 day（条件触发）
- **核心 5 day** + 实验 5-7 day = **7-12 day**（vs W0-W3 总计 22 day）

## 跨 plan 关键观察

W0-W3 共 22 day 投入：
- 工程层完美落地 ✓
- 真实瓶颈在生成端（avg_sharpe 0.29）

W5-W7 投入 7-12 day：
- 直接打生成端杠杆
- 单 day ROI 预期高于 W0-W3 后期（W2 island、W3 cost-aware bandit 都是边际改进）

**核心教训**：plan 第一阶段应聚焦"诊断真瓶颈"再设计修复，而不是按竞品 landscape 列短板优先级。如果 W0 之前先跑实测就已知 sharpe=0.29，整个 plan 应该从 prompt-quality 开始而非工程层调度。

---

**执行入口**：W5 第一步 = 改 `backend/agents/services/llm_service.py` 加 Anthropic provider 分支。当前 `OPENAI_BASE_URL=https://coding.dashscope.aliyuncs.com/v1`（Qwen）保留作 fallback。
