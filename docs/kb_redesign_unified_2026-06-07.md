# 知识库架构重设计(统一版,v1+v2 融合)

> **日期:** 2026-06-07  · **状态:** 定稿(止损期最终版)
> **方法:** 两份外部参考方案 × 两轮多 Agent workflow 对抗审查(24 agent / 2.3M token)× 代码+live-PG 实证核实
> **一句话结论:** 两份理想 KB 设计的运营模型(人类研究员 + IDE + 手动 OS 归档)与我们的系统(全自主四池 + OS 架构性隐藏)根本不是一回事;**v2「LLM-driven」相对 v1 是换皮镀金而非真进步,逃不出两堵硬墙,对「可提交 alpha + consultant 收益」净杠杆 ≈ 0**。止损期最终 KB 架构 = **不动 PG 关系底座、不上任何新库、只建 1 个聚合体检卡 + 诚实标注 + 翻 1 个 flag**,其余全部冻结。

---

## 0. 速览(TL;DR)

| 维度 | 裁决 |
|---|---|
| v1(生命周期 53 子库) | ~12 已有等价 / 1 值得新建(IS 体检卡)/ 2 零代码 / ~40 砍(OS-死 + 人类-IDE + 生成端镀金) |
| v2(机制引擎层) | 多 Agent = 给已有池阶段换名字;三库(向量/图/时序)= NEW_INFRA_HEAVY 反比例;唯一真增量 time-decay 仍 DEFER;反事实/OS-AUC = architectural-dead |
| 唯一可控杠杆 | **提交选择栈**(`robustness_selector` ∩ `marginal_drain` ∩ guard-stack G3-G10)——两份文档都没给它加任何东西 |
| 止损期该做 | ① IS 5 维体检卡(聚合,零新指标) ② 翻 `ENABLE_REGIME_MONITOR` ③ 诚实标注批处理。其余冻结 |

---

## 1. 背景与方法

- **参考方案 v1** = `wq_brain_knowledge_base_implementation.md`(按 Alpha 生命周期 8 阶段切 ~20 子库:因子谱系/墓地/regime 分类器/传导链/Cookbook/衰减档案/数据字典/陷阱库/拥挤度/失效预警/合规/归因/反愚人金/组合 Playbook/生命体征/复盘/认知偏差/诊断树/供应商评级)。
- **参考方案 v2** = `llm_driven_quant_knowledge_base_architecture.md`(神经-符号融合 / 三重表示 向量+符号+图谱 / 混合 RAG / 多 Agent / 幻觉防控 / 12 月 5 阶段路线图)。
- **方法**:两轮 Workflow(`w4mms4g0y` 评 v1 生命周期 53 子库;`wx1sqvntx` 评 v2 机制层 7 簇),每轮 = 并行映射 → 多镜头对抗批判(OS 依赖 / 运营模型 / 过度工程 / 接地核验)→ 综合。所有 EXISTS 断言强制引 file:line;载重结论再由人工代码 + live PG 核实(本会话铁律:agent 会夸大,推荐改代码前先实证)。

---

## 2. 两堵硬墙(任何 KB 重设计的边界条件)

### 墙1 — OS 架构性隐藏
BRAIN `simulate` 只返 IS。OS / Semi-OS / Real-OS / 平台 Weight / Decommissioned / 季度分红 只在**提交后**由平台盲测,**永不可读**,且 **BRAIN 不发 OS 邮件、不在页面暴露 OS**。
- 实证:`alphas.metrics` 中 `os_sharpe` 填充 = **0/14169**;13/13 提交 `os_metrics` PENDING;`#34 live OS 对账` 已判架构性死亡。
- 推论:任何以 OS / realized / Weight / 退役状态为数据源/标签的 KB 子库 = **不可建**(architectural-dead,**非 DEFER**)。v2 的"OS 预测 AUC>0.75""反事实 OS Sharpe 0.3→0.7""78% 通过概率""IMAP 读 OS 邮件"全踩此墙——且 v2 给不可读的 OS 一个**有信心的假数字**,比 v1 诚实 null **更危险**(拿 IS 当 OS 标签 = 系统化制造愚人金,与抗过拟合 180° 反向)。

### 墙2 — 无人类研究员 / IDE
AIAC = 全自主四池 LLM 流水线(HG/S/E),无人类、无 IDE、无浏览器、无 Ctrl+S。"研究员" = HG 池里的 LLM。
- 推论:凡假设 VS Code 插件 / 浏览器拦截 XHR / Slack 企微推送 / 一键插入 IDE / 用户确认 / 行为埋点 / 认知 debiasing / 代码补全请求 的组件 = REJECT。重塑成"池事件"多半也是镀金——因为池本就是这么工作的(scheduler beat → hyp_intent 已是主动触发;后端 API 直拉 = XHR 拦截的更优版;HG 自动生成入队 = 草稿箱+确认)。

### 战略约束
regime trough + 止损执行中(池已限流,`ENABLE_POOL_PIPELINE` 待重启关)。目标 = 可提交 alpha + consultant 收益。系统 **execution-limited 非 selection-limited**(13 提交 / 14169 alpha = 0.09%)→ 任何只增强**生成端供给 / 检索质量**的机制 = 低杠杆;广度 / 多样性反复被否。

---

## 3. 当前 KB 实况(2026-06-07 live PG + 代码实测)

### 3.1 存储(全 PostgreSQL 关系表,无向量/图/Mongo/Timescale)
| 表 / 类型 | 行数 | 状态 |
|---|---|---|
| `knowledge_entries` | 12153 | 🟢 |
| ├ FAILURE_PITFALL | 6259 | 🟢 live(06-07) |
| ├ SUCCESS_PATTERN | 1603 | 🟢 live(Track A) |
| ├ HYPOTHESIS_INSIGHT / FIELD_INSIGHT | 3476 / 730 | ⚪ 死(停 05-13) |
| ├ ANCHOR_METADATA / MACRO_NARRATIVE | 74 / 11 | ⚪ 低频/停 |
| `hypotheses` | 2120 | 🟢 live |
| `alpha_failures` | 40716 | 🟢 live(异质源:含 b89b732 前 FLAT + 当前池) |
| `r8_query_log` | 2211 | 🟢 live |
| `r1a_attribution_log` | 5791 | ⚪ 死(停 06-05,`ENABLE_R1A_HOOK=false`)|

### 3.2 读路径(检索分层 L0-L3)——**4 层只有 L1 真转**
`generation.py:116` `rag_service.query`(**不传 `current_expression`**)→ `query_hierarchical`(`hierarchical_rag.py`):
- **L0** exact pattern_hash / **L2** family_signature / **L3** field 级 —— 全 gate 在 `if not current_expression: return`(:411/:487/:947)
- **L1** pillar/dataset-category —— 靠 `dataset_id` 单独 fire(:701)
- **live 铁证**:`r8_query_log` layer_hits 最近 2000 次 = L1 **8709** / L0=L2=L3 **0**;`current_expression_hash` = **0/2211**。
- **根因(结构性,非 bug)**:RAG_QUERY 跑在 HYPOTHESIS/CODE_GEN 之前,此刻无表达式;self-correct(`validation.py`)走独立 Redis correction KB,也不传 expr → **四池全生命周期无任何 stage 传 `current_expression` → L0/L2/L3 真·永久死**(不是"step-1 暂时不激活")。

### 3.3 写路径(两条独立路径,勿臆造统一)
- **轨 A SUCCESS**:`persister.py` → `record_success_pattern`(`rag_service.py:1480`,3 门:有 alpha_id + 非 robustness_failed + verdict∈PASS·PROV),持久化 `expression_skeleton` + `operator_chain`(:1618)+ `dataset_categories_used` + 滚动 avg_sharpe/fitness/turnover。
- **轨 B FAILURE**:`record_failure_pattern`(`rag_service.py:1356`,signature_key=sha1(rule_id|skeleton|region) 聚类)→ FAILURE_PITFALL + alpha_failures。

### 3.4 反馈环
- 同步(E-stage 当轮写 KB)live;
- 异步认知 reconcile `cognitive_reconcile_tasks.py` flag `ENABLE_POOL_COGNITIVE_RECONCILE` 默认 OFF inert(第一行 skip)。

---

## 4. v1 生命周期 53 子库裁决

| 档 | 组件(节选) |
|---|---|
| 🟢 **已有等价(~12)** | 因子墓地/构思期注入(`negative_knowledge`→FAILURE_PITFALL + NegativeKnowledgeEnricher flag ON)、愚人金检测(V-16 + V-12)、模型失效预警(hard_gate + PROVISIONAL band + margin 经济门)、**去重提交红线**(self_corr<0.7 + auto_submit G3b/G9 + greedy_orthogonal)、**预提交 Checklist**(guard-stack G3-G10 fail-closed + submit_alpha + Redis NX)、提交归档+追踪 ID、组合暴露监控(marginal_drain)、成功/失败模式归档(persister)、全量健康矩阵(alpha_health)、模板预填充(scheduler config_snapshot)、Theme 导流(dataset bandit) |
| 🔵 **现在建(仅 1)** | IS 诊断 5 维体检卡(聚合已 live 信号,零新指标) |
| 🟡 **零代码(2)** | 翻 `ENABLE_REGIME_MONITOR`、`os_sharpe` 死优先级诚实化 |
| 🔴 **砍(~40)** | OS/Weight 依赖(architectural-dead)、人类-IDE 假设(Show Test/认知偏差/复盘叙事/诊断决策树/经济逻辑强制 gate=自证循环/参数冷却期=无状态 HG 下 moot)、阶段二全部生成端镀金(cookbook/decay 校准/窗口先验/参数敏感性)、数据字典/口径变更/供应商评级/合规库(BRAIN 是合规权威)|

> **元批评(过度工程镜头 HIGH):** 连"为这 ~40 个不可建组件写 adapted_form"本身都是镀金——把"不做"偷换成"换形态做"。止损期对永久不建项的 adapted_form 一栏一律清空。

---

## 5. v2 机制引擎层裁决

### 5.1 v2 多 Agent ↔ 已有池阶段对应表(换名字,非新建)
| v2 Agent | 已有等价(live) |
|---|---|
| Diagnostician | E-stage `evaluation.py`(V-12/V-16/CONCENTRATED_WEIGHT) |
| **Guardian** | `evaluate_guard_stack` G3-G10 fail-closed(`auto_submit_selector.py:229`)= **唯一可控提交杠杆** |
| Archivist | `persister.py` → `record_success/failure_pattern` |
| Librarian | `rag_service.query` → hierarchical_rag(L1 唯一真转) |
| Strategist | dataset bandit(`scheduler.py` weighted_choice over `dataset_cell_stats.mining_weight`) |
| Synthesizer 编排器 | **四池 DB 队列**(hyp_intent→candidate_queue→alphas)= decompose→dispatch→aggregate,确定性优于 AutoGen/CrewAI 的 LLM 聚合 |

换 AutoGen/CrewAI = 用非确定 LLM 聚合换确定性 fail-closed = **提交栈可靠性净倒退 + NEW_INFRA**。

### 5.2 三重表示存储该不该上?——**NO(全 REJECT)**
- **向量库(Milvus + CodeBERT/bge-m3)**:服务 L0/L2/L3 dormant 检索(生成端镀金),且语义相似召回**主动喂入「高 sharpe corr~1.0 近重复」愚人金**(robustness 红线)——与抗过拟合反向。
- **图库(Neo4j)**:杀手锏边(Regime-HURTS/FAILED_UNDER)需 OS 真值(墙1);谱系/SIMILAR_TO 已在 PG `hypotheses`+`family_classifier`=搬家。
- **Timescale / Mongo**:核心用例(OS 轨迹/退役复盘)踩墙1;PG JSONB+时间索引对 12153 行/13 提交规模绰绰有余。
- **符号臂(Prolog)** = `semantic_validator` + `static_checks` + guard-stack **已有且更强**(fail-closed);**AST 归一化** = `expression_to_skeleton` + `family_signature` 已有。pyswip/新库 = 重写同一层。
- 纪律:execution-limited 系统下"上一套新存储引擎"默认 NO-GO,除非证明它直接增加过得了提交闸的 alpha——v2 无一能证明。

### 5.3 混合 RAG / temporal / 反事实
- **反事实 RAG / OS-AUC** = 整簇踩墙1,architectural-dead,**从路线图永久删**(不进"未来 OS 可得再做"backlog)。
- **time-decay**(见 §6.2)= 唯一不踩墙的点,但生成端低杠杆 → DEFER。
- **查询理解层 / NER / 查询改写** = 为"人打自然语言"设计(墙2),HG 入参已是结构化 dict → REJECT。

### 5.4 neuro-symbolic / 幻觉防控 vs 已有栈
- **零结构增量**。代码层幻觉(无效算子/字段)已由 `semantic_validator`(DB-注册字段 grounding,:884-921)+ `static_checks` **确定性拦截**,最终由 **S-stage BRAIN 真模拟作终极 ground-truth**——这是 alpha 域相对 NL-RAG 域的结构性优势,使 LLM 文本一致性校验层冗余。
- 唯一架构性差异(非增量):我们是 **fail-closed 后验拒绝**(生成后 reject),非 v2 的生成时前验 grounding——但后验+真模拟**更硬,不应改**。
- **溯源链 / citation 链** = REJECT:`r1a_attribution_log` 5791 死行(有写无读)是决定性反证——同类机制已建已死=无消费者已被实证。

---

## 6. 关键裁决纠偏(人工实证,纠正两个 workflow 的偏差)

### 6.1 V-12 `os_sharpe` —— 不是 HIGH bug,是 cosmetic 死代码
- grounding 镜头标 HIGH("is_sharpe≥2 一律 reject"),综合标 build_now("改读 test_sharpe 真可得")。**两者都偏。**
- 代码(evaluation.py:225-254):`os_sh = metrics.get("os_sharpe") or metrics.get("test_sharpe") or 0`——**有 `or test_sharpe` 回退**。
- live 实证:`os_sharpe` 填充 **0/14169**(优先级1 确死)、`test_sharpe` 填充 **2532/14169(18%)**、is_sharpe≥2 共 **207** 个其中 test_sharpe>0 只 **12** 个 → 195/207(94%)高 sharpe 被 V-12 判 False。
- 但 docstring(:240)`Both null/zero → reject (no OS evidence)` = **抗过拟合有意设计**:高 IS sharpe + 零样本外证据 = 该拒。所以:
  - `os_sharpe` 优先级 = **cosmetic 死代码**(harmlessly 回退),改名 `_test_split_consistency` 即可,**行为不变**;
  - "94% 高 sharpe 被拒" = **by-design 保守**,非 bug;改读 test_sharpe **救不了**(它本身 82% 为空);
  - 真正的(超出止损期 scope 的)问题 = 是否给所有 sim 配 testPeriod split,那是另一个决策(涉 sim 成本 / consultant P0Y)。

### 6.2 time-decay —— 是生成端,不是"提交选择端"
- grounding 镜头力主 build-now 时称其"提交选择端/抗过拟合"。核实:`_score_l1_success` 在 RAG **读路径**(喂 HG 生成),是**生成端**。execution-limited(0.09%)下生成端低杠杆。
- regime 衰减红利的**正确归宿 = 提交选择侧的 `ENABLE_REGIME_MONITOR` re-sim 探针**(对 backlog/已提交集测当前-IS,检测老赢家反转 mLxlen69 IS 2.01→−0.74)。
- 裁决:time-decay = **DEFER**(若将来重启生成端,首选 `_score_l1_success` 加一行 `exp(-λΔt)` / 翻已 plumbed 的 `decayed` 布尔过滤器,纯 PG 几行,**不上 Temporal-RAG/Timescale**)。

---

## 7. 融合后·止损期最终 KB 架构

**不动 PG 关系底座、不上任何新库、不重塑墙2 组件、不为不可建组件写 adapted_form。** 只:

1. 🔵 **建 IS 5 维体检卡**(唯一放行新建):聚合已 live 的 `robustness_verdict`(`robustness_selector.py:158`)+ marginal recommendation(`marginal_analysis.py`)+ `pool_corr_by_id`(`auto_submit_selector.py`)+ V12/V16 flag → 写 `alpha.metrics['_is_diagnostic_card']` + 挂 submit-backlog 页(`ops.py`)。**零新指标、零新数据源**。剥离 v2 prompt 模板(附录 A.1/A.2)里的 OS-通过-概率/反事实字段。
2. 🟡 **翻 `ENABLE_REGIME_MONITOR`**(零代码):regime 衰减红利的正确归宿,对齐唯一杠杆;比生成端 time-decay 优先。
3. 🟡 **诚实标注批处理**(注释/标签级,不触发新工程):
   - `os_sharpe` 优先级改名 `_test_split_consistency`,注释正名 IS train/test-split 一致性;
   - `decay_curve` 在写侧(`decay_service`)+ 读侧(`alpha_health`/`regime_monitor`)统一标 IS-proxy,禁"OS/realized 衰减"措辞;
   - SUCCESS_PATTERN / FAILURE_PITFALL 加 `meta_data.verdict_basis='IS'`;
   - hierarchical_rag L0/L2/L3 标 known-dead 写进 `backend/CODE_STATUS.md`。
4. 🔴 **冻结其余全部** v2 机制层 + v1 的 ~40 个组件。

**目标分层(自主流水线下的塌缩):**
- 读路径:L1(dataset-category)保留;L0/L2/L3 标 known-dead 不复活(广度非杠杆)。
- 写路径:维持两条独立路径,加 `verdict_basis='IS'` 诚实标注。
- 反馈环:同步 live;异步 Phase 2 不翻(前置 pillar A/B 未证)。
- 8 阶段塌缩:阶段 1-4 落 HG/S/E 池内;阶段 5-8 因 OS 隐藏塌成 (a) `regime_monitor` 当前-IS 探针 + (b) E-stage 自动归档(IS-verdict 触发),其余无塌缩形态=architectural-dead。

**唯一可控杠杆仍是已 live 的提交选择栈(`robustness_selector` ∩ `marginal_drain` ∩ guard-stack G3-G10)——两份文档都没给它加任何东西。**

---

## 8. NO-GO 清单

- 向量库 / 图库 / Timescale / Mongo —— NEW_INFRA_HEAVY 对 13 提交规模反比例;杀手锏用例踩墙1 或生成端镀金。
- 多 Agent 编排重构(AutoGen/CrewAI 替四池)—— 确定性 fail-closed 换非确定 LLM 聚合 = 可靠性倒退。
- 反事实 RAG / OS 预测 AUC / 78% OS 通过 / OS Sharpe delta —— 踩墙1,architectural-dead 永久删(**不标 DEFER**,DEFER 留假希望)。
- IMAP 读 OS 邮件 / 浏览器 XHR 拦截 / git-hook / 语音转写 / 行为埋点 / Slack 推送层 —— 踩墙1(BRAIN 不发 OS)或墙2(无人类/IDE)。
- 本地部署 LLM / AST 脱敏 / 差分隐私 / RBAC 分区 —— 威胁模型不持有(无人类 IP 所有者、无多团队)。
- LLM 输出↔knowledge_entry_id citation 溯源链 —— r1a 死表实证反证。
- 让 4 层 RAG 全转(向量/语义召回)—— 生成端镀金 + 喂近重复愚人金。
- 翻 `ENABLE_POOL_COGNITIVE_RECONCILE` / 重启 settings-sweep 优化环 —— 止损期 + 前置未证。

---

## 9. 重评触发条件

- **Consultant 到位**(考核奖励)→ 切分支 A,重评 testPeriod split 配置 / 提交选择门抬升。
- **regime 转出 trough**(`regime_monitor` 当前-IS 探针 verdict=REGIME_TURNING,或当下 IS 产率恢复)→ 重启挖掘 + 重评生成端 time-decay。
- **OS 数据开放**(架构性,概率极低,~2026-07 已证伪)→ 才解锁阶段 5-8 OS 依赖簇 / 反事实 RAG / OS-AUC。
- 重评三问(任一不过即砍):**它碰提交选择吗?需新基础设施吗?踩两墙吗?**

---

## 附:证据索引
- **Workflow**:`w4mms4g0y`(v1 生命周期 53 子库,13 agent)/ `wx1sqvntx`(v2 机制层 7 簇,11 agent)。
- **代码**:`hierarchical_rag.py:411/487/701/947/1053`、`generation.py:116`、`rag_service.py:1356/1480/1618`、`evaluation.py:225-254`、`auto_submit_selector.py:229`、`brain_adapter.py:1463/1470`、`robustness_selector.py:158`、`regime_monitor.py`。
- **live PG**:`os_sharpe` 0/14169、`test_sharpe` 2532/14169、is_sharpe≥2 = 207(test_sharpe>0 仅 12)、r8 layer_hits L1=8709/L0L2L3=0、current_expression_hash 0/2211。
- **Memory**:`reference_brain_os_hidden_is_only` / `project_dev_plan_branch_b_regime_trough_2026_06_07` / `reference_wq_consultant_compensation_model` / `reference_rag_retrieval_dormant_layers` / `reference_kb_architecture_dormant_scaffolding_2026_06_05`。
