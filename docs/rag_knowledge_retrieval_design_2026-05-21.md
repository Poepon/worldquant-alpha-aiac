# 竞品分析 — 知识检索(RAG)环节应该怎么设计

> **文档日期**:2026-05-21(**v2,经 2 路 fresh-agent 对抗性审查修订**,见 §7)
> **承前**:[`competitive_analysis_v2_2026-05-19.md`](competitive_analysis_v2_2026-05-19.md)(系统层 25 系统对比)
> **本文聚焦**:挖矿流程"第 1 步:知识检索"(RAG_QUERY)这一个环节 —— 各家怎么设计、AIAC 现状的实证缺陷、目标设计与路线
> **触发**:生产观察到"第 1 步参考模式/避坑指南内容恒定不变",根因定位后做的横向设计调研
> **前提**:本文描述的 bug **仅在 `ENABLE_HIERARCHICAL_RAG` override 为 ON 时成立**(`config.py:596` 默认 `False`)。该 flag 自 2026-05-18 被 DB override 置 ON,故现网命中。flag OFF 时走 legacy 路径,不发生 L1 坍缩。

---

## 0. 三条 take-away

1. **AIAC 的"hierarchical RAG"与同名的 Alpha-GPT 方向相反、且在第 1 步退化为单层**。Alpha-GPT 是 *agent 主动 top-down 导航*(类目→子类→字段,生成**之前**用来在数万字段里收敛搜索空间);AIAC 是 *被动 fall-through 检索*(精确→pillar→family→字段,specific→broad)。第 1 步没有表达式时 **只有 L1-pillar 能 fire**,且 L1 是 `ORDER BY id DESC` + **完全忽略 dataset_id** → 返回该 pillar「最新 N 条」(N≤budget=5)→ **同一 region 内所有数据集拿到相同内容**。这就是生产里"参考模式/避坑指南都一样"的根因。(不同 region 因 pillar 推断不同,内容会变。)

2. **AIAC 检索无 embedding、无语义相似度;相关性靠精确匹配 + Python 端加权打分**。匹配手段是 JSONB `@>` containment / 文本 ILIKE / hash 相等。relevance 排序确实存在(legacy 的 composite score、L2 的 R5 历史分),但**第 1 步命中的 L1 这一层恰恰没有任何相关性排序,只有 `id DESC`**——这正是 bug 所在。对比 2026 业界标配 **dense 向量 + sparse(BM25)+ 全文 三路 hybrid + RRF 融合 + reranker**,AIAC 在"语义检索"这一维仍是空白。

3. **检索质量直接决定挖矿产出,不是锦上添花**。FactorMiner 自家 ablation:带 Experience-Memory 检索 **60% 高质量产出 vs 无 memory 20%**(在其自有 pipeline 上)。这是一个方向性佐证(检索 steering 有效),**不能直接外推为 AIAC 上 pgvector 后的预期收益**——AIAC 并非 memory-less baseline(已有 3k+ KB + composite 打分 + dual-channel)。但当前第 1 步给 LLM 的是"恒定、与数据集无关"的先验,确实削弱了检索的 steering 作用。

---

## 1. AIAC 现状(代码级实证,2026-05-21)

### 1.1 KB schema

单表 `knowledge_entries`(`backend/models/knowledge.py:29-66`):

| 列 | 作用 |
|---|---|
| `entry_type` | `SUCCESS_PATTERN` / `FAILURE_PITFALL` / `ANCHOR_METADATA`(后者排除检索) |
| `pattern` (Text) | alpha 表达式 skeleton |
| `pattern_hash` (UNIQUE) | `sha256[:32](pattern\|region\|dataset_id)`,L0 精确匹配键 |
| `description` | 文本描述 |
| `meta_data` (JSONB) | `pillar_classified`(L1 键)/ `family_signature`(L2 键)/ `regions` / `hypothesis_ids` / `decayed` / `failure_tree` / `sources` / 运行均值(`avg_sharpe`/`expected_sharpe`/`usage_count`)等 |

**没有任何 embedding / 向量列。** 三个索引:
- `pattern_hash` UNIQUE
- `ix_kb_meta_data_gin` —— `GIN (meta_data jsonb_path_ops)`(`alembic/versions/b3c8d9e2f4a1_...py`)。**L1/L2 的 `@>` containment 全靠它**,是本文分析的关键依赖。
- `ix_kb_failure_pattern` —— **partial B-tree on `pattern`**(`postgresql_where = entry_type='FAILURE_PITFALL'`,`knowledge.py:44-48`),不是 GIN。

(数据库仅 provision 了 `pgcrypto` 一个扩展;**没有 vector 扩展** —— 见 §4.2。)

### 1.2 检索入口与路由

- `node_rag_query`(`generation.py:99`):每轮**开头**调一次,传 `dataset_id, region, max_patterns=5, max_pitfalls=10, hypothesis_id`。**第 1 步不传 `current_expression`**(表达式要到第 4 步 CODE_GEN 才生成)。
- `rag_service.query()`(`rag_service.py:330`):`ENABLE_HIERARCHICAL_RAG` ON 且有 `(expression 或 pillar)` → 走 `query_hierarchical`;否则走 legacy。
- **G4 补强**:hierarchical ON 但第 1 步无 expression/pillar 时,`_infer_pillar_hint_from_pool(region)`(`rag_service.py:516`)从 7 天 alpha 池缺口推一个 pillar(per-region,Redis 60s cache,session 内基本恒定但随池缓慢漂移)。**注意它是条件触发**:缺口低于阈值时返回 `None`,推出 `"other"` 时 L1 短路返回空(`hierarchical_rag.py:600-602`)→ 这两种情况会 fall through 回 legacy。所以"L1 必 fire"不是绝对,但缺口存在时(现网常态)即坍缩成本文描述的 bug。

### 1.3 四层及其排序

| 层 | 触发条件 | 匹配方式 | 排序 | 第 1 步能否 fire |
|---|---|---|---|---|
| **L0 精确** | 需 expression | `pattern_hash =` 相等 | `id DESC` | ❌ 无表达式 |
| **L1 pillar** | 需 pillar | `meta_data @> {pillar_classified}` | **`id DESC`(无相关性排序)** | ✅ **唯一能 fire** |
| **L2 family** | 需 expression | `meta_data @> {family_signature}` | `id DESC` + **R5 历史分 rerank** | ❌ |
| **L3 field** | 需 expression | `pattern ILIKE %field%` | `id DESC` | ❌ |

- **L2 的"R5 rerank"是 SQL 查 `r1a_attribution_log` 的 `AVG(r5_composite_score) GROUP BY expression_hash`**(`hierarchical_rag.py:862-884` + `fetch_r5_avg_scores`),**零 LLM 调用**——是廉价历史分排序,**不是** R5 LLM judge(后者评的是 simulated alpha,见 §4.3)。`ENABLE_R5_L2_RANKING` 已退役并并入 `ENABLE_HIERARCHICAL_RAG`。
- **legacy 路径**(flag OFF):`SELECT … ORDER BY id DESC LIMIT 800` 取候选窗,再 Python composite score(SUCCESS:dataset 命中 + category + region + `expected_sharpe` + `usage` + hypothesis-family,`rag_service.py:655-704`)排序取 top-K。即 `id DESC` 只是候选窗,真正排序是 composite score。

### 1.4 "内容恒定"的因果链

```
ENABLE_HIERARCHICAL_RAG=ON(override)
  → 第1步无表达式 → 只有 L1 能 fire → pillar 来自 pool-deficit(per-region,session 内基本恒定)
  → L1 SQL: WHERE meta_data @> {pillar} ORDER BY id DESC LIMIT N   ← 无 dataset 过滤、无相关性排序
  → 返回该 pillar「最新 N≤5 条」,与 dataset_id 完全无关
  → 同一 region 内不管 option9 / pv1 / fundamental6 / news18,参考模式 + 避坑指南全相同
```

> **legacy 是否"按 dataset 变化"?——只部分,且不保证**(审查修正):
> - legacy **SUCCESS** 有一个 `RAG_SCORE_DATASET_MATCH` 加分项(`rag_service.py:655-659`),但它只是 ~6 个加项之一,且仅当 recency-capped 800 行候选里**恰好有 dataset-stamped 命中行**才生效——多数 KB 行是 seed/external/category-tagged,未按 dataset 打标,该项常为 0,top-K 由 sharpe/usage/category 主导(dataset-invariant)。
> - legacy **FAILURE** 路径(`rag_service.py:786-822`)**完全没有 dataset 项**,只按 severity/category/error_type/hypothesis-family。
> 所以 legacy 比 hierarchical-L1 略好(success 侧有数据集倾向),但**不是可靠的 dataset-aware 检索**。这弱化了把"fall back legacy"当 P0 的吸引力(见 §4.1)。

---

## 2. 竞品怎么设计知识检索

### 2.1 设计矩阵

| 系统 | 检索范式 | 相关性机制 | 正/负通道 | 时机 | 闭环 |
|---|---|---|---|---|---|
| **AIAC**(现状) | 被动 fall-through(specific→broad) | 精确匹配 + Python 加权(legacy/L2);**第 1 步 L1 仅 id DESC** | R4' dual-channel(已有) | 生成**前**单发 | ingest 单向 |
| **Alpha-GPT v2** | **agent 主动 top-down 导航**(broad→narrow) | LLM 自主选类目,管 context | 学已成功 alpha(RAG#0) | 生成**前**多步导航(navigate-to-decide) | human-in-loop |
| **Hubble v2** | dual-channel RAG + family-aware select | formula-similarity 惩罚 | **positive + negative 双通道**("avoid like") | 生成时 | persistent diagnostics |
| **FactorMiner** | Experience-Memory(Ralph Loop) | context-dependent memory signal,导向**正交区** | success pattern + **forbidden region** | 检索→生成→评估→蒸馏 | **闭环回写 memory** |
| **RD-Agent** | cross-task hypothesis forest | bandit 方向 reward | — | hypothesis 阶段 | DAG trace 回写 |
| **AlphaLogics** | logic-as-asset 反向挖矿 | logic 库每周演化 | — | 生成前 | **PASS alpha → 抽 logic → 反哺** |
| **2026 RAG 工程** | **hybrid:dense+sparse+全文** | **RRF 融合 + reranker(ColBERT/LLM)** | — | query-rewrite → retrieve → rerank | — |

### 2.2 深挖

**Alpha-GPT v2(EMNLP 2025)** — 同名机制的真实形态:
agent 先 RAG#0 学习历史成功 alpha 的特征,再**主动查询高层类目**(Price-Volume / Sentiment)锁定方向,再聚焦二级类目(Earnings Call),最后取**具体数据字段**详情,然后才生成。核心目的是 **在数万字段里管理 context 窗口**,**边导航边产 idea(navigate-to-decide)**。是 agent 行为,不是一次 SQL。AIAC 借了名字,丢了 agent 导航与"管 context"的本意。

**Hubble v2(arxiv 2604.09601, 2026-04)** — 安全/多样性导向:
DSL 约束生成 + AST 沙箱 + **dual-channel RAG(正例 + 负例"avoid like")** + formula-similarity 惩罚 + **family-aware selection**。AIAC 的 R4' dual-channel / R10 family-cap 对齐它,方向正确但仍是精确匹配。

**FactorMiner(arxiv 2602.14670, 2026-02)** — 检索即 yield 引擎:
Experience Memory 存 **success pattern** + **forbidden region**(与库内已有高互相关的因子族,如 VWAP 偏离变体)。Ralph Loop(Retrieve→Generate→Evaluate→**Distill 回写**)。检索出 **context-dependent memory signal,作为 prompt 级约束把探索导向正交、高期望产出区**。其 ablation **带 memory 60% vs 无 memory 20%**(自有 pipeline)——方向性证明检索 steering 有效,**不可直接外推 AIAC**(见 §0 #3 caveat)。

**2026 RAG 工程主流**:naive 单路检索已被淘汰 —— 标配 **dense 向量 + sparse(BM25)+ 全文 三路 hybrid**,**RRF**(`rrf_k≈60`,每路 top-k≈20)融合,再上 **reranker**(ColBERT / LLM cross-encoder);查询前常做 **query rewriting**。研究指出"小 embedding + LLM rerank"常胜过大模型纯向量。AIAC 在向量这一维为空白。

---

## 3. AIAC 的 gap(7 维)

| # | 维度 | 业界做法 | AIAC 现状 | 严重度 |
|---|---|---|---|---|
| G1 | **语义相关性** | dense 向量 + hybrid + rerank | 精确匹配 + Python 加权,无 embedding | 🔴 高 |
| G2 | **第 1 步可用信号** | Alpha-GPT agent 导航 / FactorMiner memory signal | L1-only,`id DESC`,与 dataset 无关 | 🔴 高(当前 bug) |
| G3 | **检索时机** | 生成前导航 + 生成中/自纠时再检索 | 仅每轮开头单发,self-correct 不检索 | 🟡 中 |
| G4 | **正交/反样本导向** | forbidden region 主动避重(FactorMiner) | R4' 负通道有,但无"正交区"steering | 🟡 中(R10-v2 已规划) |
| G5 | **agent 主动性** | LLM 自主 top-down 导航 (Alpha-GPT) | 被动单次 SQL | 🟡 中 |
| G6 | **闭环回写** | PASS→抽 logic→反哺(AlphaLogics) | ingest 单向 | 🟢 低(G10 已规划) |
| G7 | **dataset 粒度** | 字段级语义检索 | L1 完全忽略 dataset_id(无该入参) | 🔴 高(当前 bug) |

---

## 4. 目标设计:知识检索环节应该怎么做

分四层递进。**P0 在 L1 源头修当前 bug;P1 补语义底座;P1.5 精排;P2 对齐 SOTA。**

### 4.1 P0(立即,修症状)— 让第 1 步重新有 dataset 区分度

> ⚠️ **审查否决了旧 v1 的"(b) 把 RAG 时机后移到 hypothesis 之后"**:`node_rag_query` 的产出 `state.patterns/pitfalls` **正是** distill_context(`generation.py:211`)+ hypothesis(`generation.py:1005,1011`)的 prompt 输入。把 RAG 移到 hypothesis 之后会**饿死 hypothesis**(拿不到任何 pattern/pitfall)。且"先决策后检索"与 Alpha-GPT 的"navigate-to-decide"方向相反。故 **(b)-as-move 作废**。

推荐 **(c) 在 L1 源头修**(真根因修复):

- `layer1_pillar` 当前签名 `(current_expression, hypothesis_pillar, region, budget)` —— **没有 dataset_id 入参**(orchestrator 也没往下传)。P0:把 `dataset_id` / `dataset_category` 一路 thread 进 `layer1_pillar`,作为 **soft 打分项**(不是 hard `WHERE` —— 多数行无 dataset stamp,硬过滤会清空结果),并把 `ORDER BY id DESC` 换成 **pillar 命中集内按 `expected_sharpe`/`usage`(JSONB 取值,Python 端排序)+ 轻随机** 选取。本质是把 legacy 已有的 composite 打分逻辑,scoped 到 pillar 命中集。
- 效果:第 1 步对同 region 不同 dataset 给出不同且更相关的 patterns;同时拿掉"永远最新一条"的死板。
- 备选 **(a) fall back legacy**:实现更小(`query()` 加分支),但据 §1.4 修正,legacy 只有 success 侧部分 dataset-aware、failure 侧完全不 aware —— 是治标的次选,不如 (c) 治本。

> 注:`expected_sharpe` 在 `meta_data` JSONB 里,不是列;SQL 端 `ORDER BY` 需 `meta_data->>'expected_sharpe'` 转型且无索引,故建议在 Python 端排序(候选集 ≤ budget*2,量小)。

### 4.2 P1(补语义底座)— pgvector + hybrid 检索 ⚠️ **新增基础设施,非"零成本"**

> 审查修正:旧 v1 写"pgvector 零新增基础设施"是**错的**。`docker-compose.yml` 用 `postgres:15-alpine`(不含 vector 扩展),全 repo 仅 provision 了 `pgcrypto`(`alembic/.../81171bee8f91:36`),无 DB Dockerfile。pgvector 是 **server-side C 扩展**,"在 PostgreSQL 上"≠"扩展已装"。

落地实际需要:

1. **DB 扩展**:换 `pgvector/pgvector:pg15` 镜像(或为 alpine 编译扩展)+ Alembic `CREATE EXTENSION vector` migration。**Windows 是主开发平台**(CLAUDE.md),pgvector 在 Windows 非 `pip install`,要么换 Docker 镜像、要么手动编译放 `lib/` —— 这是真实的 infra 迁移 + dev-setup 文档更新。
2. **Embedding 能力(当前不存在,必须新建)**:`LLMService` 只有 chat completion,**无 `embed()`**;`requirements.txt` **无 embedding 库**;`config.py` **无 embedding 配置**;**DeepSeek(默认 provider)无 embeddings 端点**。二选一:
   - 接 OpenAI/其他 vendor 的 embeddings(新 key + 新成本线 + `LLMService` 新 plumbing),或
   - 引入 `sentence-transformers`(重依赖:torch/transformers 数百 MB + 模型下载 + 批量推理 worker;在 `--pool=solo` Celery 上要单独跑)。
3. `knowledge_entries` 加 `embedding vector(N)` 列 + ivfflat/hnsw 索引;ingest(record_success/failure + external)生成 embedding;**回填存量**(count 见下)。
4. 检索改 **hybrid**:结构化过滤(region/dataset/pillar)**+** 向量相似度,用 **RRF** 融合,取代纯 `id DESC`。

> 存量规模:代码注释(2026-05-13)称 ~180 SUCCESS / ~1660 FAILURE(`rag_service.py:617,755`),MEMORY 索引另记 KB 总量 ~3357 行 —— 均为注释/历史值,**回填前需 live count 核实**。

### 4.3 P1.5(精排)— LLM reranker(**新建,非"复用 R5"**)

> 审查修正:旧 v1 "复用 R5 LLM judge"是张冠李戴。L2 现有的"R5 rerank"是 SQL 历史分查询(§1.3),不是 LLM judge。真 R5 LLM judge(`r5_judge.py:166`)要 `(hypothesis_statement, description, expression)` 三元组、实现 AlphaAgent Eq.7 的 alpha 对齐,**绑定评 simulated alpha**;而检索精排是评 `(hypothesis, 候选 pattern)` 相关性——**c₂ bridge 在第 1 步无 expression/description 时未定义**。

所以这是 **新 prompt + 新 (h, candidate) 打分路径 + abstain/成本守卫**,只复用 LLM 调用 plumbing。**成本**:top-20 逐条 rerank = 每轮最多 +20 次 LLM round-trip,叠加既有 hypothesis/code_gen/self_correct/R5 调用 —— 即便 haiku 档,调用**次数**与串行**延迟**都放大热循环。需 batch + 成本 guard + 延迟 mitigation。**工时按新建估,不是 1.5 人日。**

### 4.4 P2(对齐 SOTA 范式)— agentic 导航 + 闭环

- **Alpha-GPT 式 agent 导航**(对齐同名机制本意,navigate-to-decide):探索阶段让 LLM 自主"选类目→选字段"多步导航 KB。**注意与 P0 的关系**:P0 选 (c)/(a) 不锁死 workflow 形态,P2a 可后续叠加;若 P0 误选了"移动 RAG"则会与 P2a 的"边导航边决策"冲突——这也是否决 (b) 的另一理由。
- **新增"第二次检索"(原 v1 (b) 的正确形态)**:**保留**第 1 步 pre-hypothesis RAG(喂 hypothesis),**额外加**一次 post-hypothesis / pre-code_gen 的 enriched 检索(此时有 pillar + 候选字段,L1/L3 都能 fire),喂 code_gen。是"add",不是"move"。
- **FactorMiner forbidden-region steering** —— 与已规划 **R10-v2 hard forbidden region** 合并。
- **AlphaLogics 闭环** —— 与已规划 **G10 logic-as-asset 反向蒸馏** 合并。

---

## 5. 路线与优先级

| 优先级 | 项 | 工时(修正后) | 修哪个 gap | 依赖 |
|---|---|---|---|---|
| **P0** | 4.1 (c) L1 源头 thread dataset_id + 去 recency 排序(或 (a) fallback) | 1-2 人日 | G2/G7 | 无,立即可做 |
| **P1** | 4.2 pgvector **扩展安装 + embedding provider** + hybrid + RRF + 回填 | **≥10 人日**(infra + 新依赖 + 回填,远超旧估 4-5) | G1 | DB 镜像/扩展 + embedding 选型 |
| **P1.5** | 4.3 **新建** LLM reranker + batch/成本守卫 | 3-4 人日 | G1 | P1 落地后 |
| **P2** | 4.4 第二次检索(add)+ agent 导航 | 3-4 人日 | G3/G5 | P1 |
| **P2** | forbidden-region(合并 R10-v2) | 2 人日 | G4 | R10-v2 |
| **P2** | 闭环蒸馏(合并 G10) | 4 人日 | G6 | G10 |

**建议序**:先 **P0 (c)** 本周止血(flag-gated,符合 default-OFF + 观察期文化,且不锁死 workflow 形态)→ 评估 P1 的 infra 成本后再决定是否上 pgvector(这是最大投入,需单独 plan 含镜像/扩展/embedding 选型 + Windows dev 路径)→ P1.5 → P2 顺 G10/R10-v2 既有路线合并。

> **不学**:全套 PPO/RL 检索(TLRS)—— BRAIN 日级 sim 限额下不划算;naive 大向量模型 —— 业界已验证小 embedding + LLM rerank 更优。

---

## 6. Sources

- Alpha-GPT v2(EMNLP 2025 Demos)— https://aclanthology.org/2025.emnlp-demos.14.pdf / https://arxiv.org/html/2308.00016v2
- Hubble v2(arxiv 2604.09601, 2026-04-14)— https://arxiv.org/abs/2604.09601(本 session WebSearch 核实)
- FactorMiner(arxiv 2602.14670, 2026-02)— https://arxiv.org/abs/2602.14670(60/20 ablation 出处;本 session WebSearch 核实)
- 2026 hybrid RAG 工程实践 — https://superlinked.com/vectorhub/articles/optimizing-rag-with-hybrid-search-reranking / https://infiniflow.org/blog/best-hybrid-search-solution / "Rethinking Hybrid Retrieval" https://arxiv.org/pdf/2506.00049
- AIAC 内部对照源 — `backend/models/knowledge.py` / `backend/agents/services/rag_service.py` / `backend/agents/hierarchical_rag.py` / `backend/agents/graph/nodes/generation.py` / `backend/agents/graph/workflow.py` / `r5_judge.py` / `config.py` / `docker-compose.yml` / `alembic/versions/`

---

## 7. 对抗性审查 log(2026-05-21)

2 路独立 fresh-agent 逐行核对代码后修订。**核心诊断(L1-only @ 第1步 / `id DESC` / 忽略 dataset_id / 无 embedding)被双方判 SOUND**;以下为已修正项:

**MUST-FIX(改了结论)**
1. 旧 §4.1(b)"移动 RAG 到 hypothesis 之后" **作废** —— RAG 产出喂 distill_context+hypothesis(`generation.py:211/1005/1011`),移动会饿死 hypothesis;且方向与 Alpha-GPT 相反。改为 P0=(c) L1 源头修,(b) 正确形态降级为 P2"add 第二次检索"。
2. §4.2 "pgvector 零基础设施" **更正为新增 infra** —— `postgres:15-alpine` 无 vector 扩展,仅 `pgcrypto` 已 provision;Windows 主平台安装非平凡。P1 工时 4-5→≥10 人日。
3. §4.2 embedding endpoint **更正为不存在** —— 无 `embed()`/无库/无配置/DeepSeek 无 embeddings;需新 vendor 或重依赖。
4. §4.3 "复用 R5 judge" **更正** —— L2 现有 R5 rerank 是 SQL 历史分,非 LLM judge;真 judge 绑定评 alpha、需三元组,精排是新建。工时 1.5→3-4 人日。
5. §0 #2 "100% recency / 零相关性" **窄化** —— legacy 有 composite 打分、L2 有历史分 rerank;bug 精确定位为"第 1 步 L1 层 `id DESC` 无相关性 + 忽略 dataset"。
6. §1.1 索引 **补全/更正** —— 补 `ix_kb_meta_data_gin`(GIN,L1/L2 @> 依赖它);`ix_kb_failure_pattern` 是 partial **B-tree** on `pattern`,非"partial GIN"。
7. 全文 **补 flag-gated 前提** —— bug 仅 `ENABLE_HIERARCHICAL_RAG` override ON 时成立(默认 False)。
8. §1.4 "legacy 按 dataset 变化" **更正** —— 仅 success 侧部分、failure 侧完全无 dataset 项,且不保证生效。

**SHOULD-FIX**:FactorMiner 60/20 标注为自有 ablation、不外推 AIAC(§0#3/§2.2);L1 条件触发(可返回 None / "other" 短路);`expected_sharpe` 是 JSONB key 非列(§4.1 注);存量 count 标注为注释/待核实;"最新一条"→"最新 N≤5 条";作用域限"同 region 内"。

---

*本文聚焦知识检索单环节,是 v2 系统层竞品分析的纵深补充。下一步:P0 (c) 可直接实施;P1 pgvector 是最大投入,需单独 plan 评估 infra/embedding 选型后再决策。*
