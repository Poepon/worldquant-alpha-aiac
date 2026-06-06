# KB / 接线 / 反馈环 —— 池原生重设计 (Pool Phase 2)

> 2026-06-06 · v2(双 workflow + 两轮对抗证伪后)
> 方法:live Postgres 实查 + workflow①(`wy32yozh5`,映射池化后真实接线 + ROI gap + 对抗证伪)+ workflow②(`w2ecldgxe`,3 视角池原生设计 → 综合 → 7 承重决策对抗证伪)+ 人工核验承重根因。
> 框架(用户锁定):**在池架构下原生设计、抛弃 FLAT 包袱;按 6 层量化流程归属 + 4 层 KB(战略/战术/执行/认知)划分。**
> 取向(用户):挑战 execution-limited 先验、投深度 —— 但**对抗证伪表明深度的数据还不存在,Phase 1 先建脊柱 + 仪表,深度成为数据驱动的 Phase 2。**

---

## 0. TL;DR

**核心命题:池拓扑本身已经是 6 层量化流程的干净实现**(每层有明确 pool/beat 拥有者),真正缺的只有两块:**组合层的反馈回灌** 和 **认知层(整层)**。重设计 = 用池原生的 DB-queue + 异步 beat 机制补这两块,把 FLAT 的 in-graph / round / mode-flag 机制整体丢掉。

**最小落地 = 2 个池内原语 + 1 个异步 beat:**
1. **LB1** HG 无条件建 `hypotheses` 行(删 `hge_level` gate)→ 复活 `current_hypothesis_id` FK 脊柱(零迁移,链全已接)。
2. **LB3** persister 写 `SUCCESS_PATTERN`(从死的 node_save_results 抽出)→ 闭合读写不对称(✅ 唯一全过对抗审查)。
3. **LB2** 新建 `run_pool_cognitive_reconcile` beat → 唯一认知引擎(生命周期 + stats + 归因),读 DB 行驱动,替代所有 in-graph round-scoped feedback。

**⚠️ 最重要修正(LB4 flawed):** 我原先的"submit-gate orthogonal-yield reward 是 execution-limited 下最高杠杆深度"被**实查数据证伪** —— 史上**全门只 1 个 alpha**(18 can_submit→2 非 SKIP→1 过 self_corr),且该 reward v1 早因稀疏被 rolled back,killswitch 还休眠。**结论:reward/PROMOTE 保持 binary can_submit;orthogonal-yield 只能作降级-open 的二级 tie-break,且 ~2026-07 有 OS 数据前不能宣称受 killswitch 守护。**

**诚实的深度账:** "投深度"被压力测试后收敛为 —— **深度的数据还不存在**(1 个全门 alpha、0 个 OS 对、0 个归因 stamp)。所以 Phase 1 建**脊柱 + 仪表**(让系统开始产出"归因分布 / submit-yield 标签 / 生命周期"这些数据),Phase 2 的深度(retry/mutate、表达式 RAG)**gate 在这些数据上**。这既兑现"投深度"(建向它),又尊重"先实证"。

---

## 1. 6 层量化流程 × 池拓扑归属

**关键洞察:零新增 worker。** 6 层各有现成拥有者;唯一热路径改动是 HG 里一个 soft-fail INSERT;所有认知搬到异步 beat。

| 量化层 | 池里谁拥有 | 池原生设计 + 丢弃的 FLAT 机制 |
|---|---|---|
| **数据** Data | sync beats(目录 06:00 / alpha 6h)+ alpha_pnl ingest + scheduler `mining_weight` 加权采样 + HG hydrate(字段/算子) | **不变**,已 ~85% 池原生、beat 驱动、从不 round 驱动 → 零包袱 |
| **特征** Feature | HG worker(`node_rag_query` RAG read + distill + hypothesis + codegen) | RAG read **早 live**(构造注入 `RAGService(wdb)`,1800 L1 命中/2d);L0/L2/L3 暗是**时序**所致(检索在表达式存在前)非缺注入 → 见 §8 Phase 2 第二跳。FieldScreener 保持删除 |
| **模型** Model | HG(生成)/ S(模拟,K_S=2 稀缺 BRAIN 槽)/ E(评估,K_E=1) | **承重改动在 HG**:删 `hge_level` gate,建 Hypothesis 行变无条件(LB1)。S/E **字节不变**。node_evaluate **不写任何反馈/KB**(守 idle-in-txn + 不独占 E 槽);只在 alpha_failures.metrics stamp 一个廉价 failure_class |
| **组合** Portfolio | **异步 reconcile beats**(读 landed alpha + marginal + self_corr)+ self_corr<0.7 门(E inline)+ marginal_drain/recon | 扩展 `refresh_can_submit_for_alpha`:can_submit 翻 True 时算 marginal verdict + self_corr_vs_pool + margin_bps,stamp 单个 `_submit_yield_label` JSONB(仿 `_iqc_marginal`,**无迁移**)。这个 dense label **今天就在 67 个积压行上**。权重优化器/风险模型出范围 |
| **交易** Trading | auto_submit beat(mode=live,G0-G10 fail-closed)+ submit_alpha + 手工 backlog 页 | **门机器不变**(真瓶颈、最后一公里已对)。架构只**喂更好的候选**:reconcile-beat 读 SUBMITTED 行作顶点 ground-truth → 标 parent Hypothesis PROMOTED-on-submit + SUCCESS_PATTERN submitted=True |
| **监控** Monitoring | lease-recycle(*/2min 单一恢复)+ canary + supervisor + ops + **新 reconcile-beat 的认知/yield 遥测** | 恢复/健康**不变**。**新增** submit-yield + 认知健康面(生命周期分布 / 变异链深度 / 归因混合 / per-dataset orthogonal-yield 榜 / backlog 燃尽 / marginal_recon 符号一致作 META-killswitch) |

---

## 2. KB 4 层 × 池原生

| KB 层 | 池原生设计 | 写 | 读 |
|---|---|---|---|
| **战略** Strategic | scheduler `mining_weight` bandit(已 live)= "挖什么/往哪挖"。reward **保持 binary can_submit**(LB4:orthogonal-yield 统计死)。复用 discounted Beta-Bernoulli + 冷启 verbatim。 | `dataset_weight_refresh` beat 写 mining_weight | scheduler 每 */5min 读 cell mining_weight;HG 读 list_active/fetch_cross_task_promoted(G8 假设森林,已存在) |
| **战术** Tactical | HG 的 RAG read + operator_prefs + blacklist。**闭合写对称**:`SUCCESS_PATTERN` 从死的 node_save_results 抽进 persister(LB3)。submit-gate 标签进 `meta_data` 二级(降级-open,非主信号)。 | persister 每 PASS/PROV mint SUCCESS_PATTERN;FAILURE_PITFALL 写者不变(refresh + 日 negative_knowledge beat)+ 加 meta_data.hypothesis_id | HG `node_rag_query`/`node_code_gen` prompt:SUCCESS(可按 submit_cleared 二级排序)+ FAILURE + MACRO few-shot |
| **执行** Execution | **按成本两层 IO**:(1) 廉价同步池内写 = persister UPSERT alphas/failures/trace + 抽回的 record_success_pattern(自开 AsyncSessionLocal,隔离 commit,守 F1);(2) 贵的异步跨行 reconcile = can_submit refresh / marginal / self_corr / meta 富化 / 生命周期 全在 beats,**绝不在 node_evaluate**。 | E persister(同步,隔离 commit per F1)+ beats(异步,离 E 槽) | reconcile-beat SELECT 近窗 landed alpha(can_submit/marginal/self_corr)+ candidate_queue(DONE since watermark)JOIN on hypothesis_id |
| **认知** Cognitive | **一等公民,由单个新异步 beat `run_pool_cognitive_reconcile`(~15-30min)驱动,替代所有 in-graph round-scoped feedback**。per 被触及 hypothesis_id(DB JOIN 读,非内存 round):(1) 生命周期 via 现存幂等方法 auto_activate + refresh_stats + 新 round-less abandon;(2) 归因 = 廉价 early_stop.classify_attribution(Phase 1)→ classify_attribution_llm 聚合(Phase 2);(3) Phase 2 retry/mutate 作新队列行。 | reconcile-beat:hypotheses 生命周期 + denorm stats + 归因 stamp + SUCCESS meta 富化 + Phase 2 队列行 | candidate_queue(DONE,watermark)+ alphas/failures JOIN hypothesis_id;marginal_recon.sign_agreement 作 killswitch 输入 |

---

## 3. FLAT 包袱丢弃清单 + 池原生替代

| 丢弃(FLAT 机制) | 池原生替代 |
|---|---|
| `hypothesis_centric_level` level-gate(generation.py:711/821 的 mode 开关) | **删 gate**,建 Hypothesis 行变 HG **无条件**步骤(只 lift INSERT 块 821-930,**不要 un-gate** V-22.13)。零迁移。 |
| V-22.13 跨轮复用(generation.py:739-812,靠 `state.hypothesis_round_history`) | **整删**(池跨进程不携带 round_history → 恒 path_a_no_state → 构造性死)。跨候选累积自然发生(每候选链 DB hypothesis 行,refresh_all_stats 全生命周期聚合)。lineage 复用 = beat 用 `seed_parent_hypothesis_id` 种新 hyp_intent(Phase 2) |
| round-scoped `_process_hypothesis_feedback` + `upsert_round_stats`(键 hyp_id,round_index,task_id) | **删 round-keyed 路**(池无 round_index、单一常驻 task_id → 唯一键塌缩)。换 reconcile-beat 的 `refresh_all_stats`(直接从 alphas/failures JOIN 重算,无 round 桶)。`HypothesisRoundStats` **deprecate 不删**(留 legacy 读) |
| in-graph r1b retry/mutate cycle(workflow.py:191-250)+ in-state `r1b_retries_attempted_this_alpha` guard | **删 in-graph cycle**(池只编译 `_build_evaluate_graph`,结构性不可达;in-state guard 跨 candidate_queue 边界不存活)。**留** r1b_loop.py 的 `node_code_gen_retry`/`node_hypothesis_mutate` 函数(只 import config,不 ImportError)。Phase 2 替代 = beat INSERT 新 candidate/hyp_intent 行(forward-only re-mining,depth 在 context) |
| `node_save_results`(FLAT per-round persist+feedback omnibus,不在任何池子图) | **按成本拆,不复活**:record_success_pattern → persister(廉价同步 Tactical 写);生命周期 feedback → reconcile-beat(异步 Cognitive)。然后**删** node_save_results |
| binary can_submit reward + pass_count>0 PROMOTE **改 orthogonal-yield** | ❌ **驳回作主信号(LB4,见 §6)**。reward/PROMOTE **保持 binary can_submit**;orthogonal-yield 只作降级-open 二级 tie-break(positive class <~10/window 时退 binary)|
| agents/core pickle CoSTEER + r5_judge + feedback_g5/r1b + genetic + evolution_strategy + FieldScreener + R1a/R5 块 | **保持已删**(b89b732 已物删/tombstone)。reconcile-beat 的 DB-row 驱动生命周期 + 存活的 attribution_types/early_stop/hypothesis_service 方法覆盖认知脊柱,无需复活任何 pickle-DAG / 同步-in-eval 机制 |

---

## 4. 承重决策对抗证伪记分卡(workflow② 7 决策)

| | 判定 | 必带守卫(对抗审查要求) |
|---|---|---|
| **LB1** HG 无条件建 hypotheses 行 | risky-needs-guard | **必须和 reconcile-beat 同发**(单发→僵尸 PROPOSED,重现 V-22.13"0 abandoned");只 lift INSERT(821-930)别 un-gate V-22.13(739);intent dedup key 防 lease-recycle 重建;auto_promote 现按 pass_count>0 会 promote on PASS_PROVISIONAL → 须改判据 |
| **LB2** 认知全异步 beat,node_evaluate 零同步认知 | risky-needs-guard | **beat 按 watermark 只扫近窗 landed alpha,不 refresh_all_stats 全 active**(否则随 ~207/日线性变慢);真因是 K_E=1 槽独占 + 跨进程态丢失(非 idle-in-txn);coupled lift 加一个同步 filter_terminal_ids/save(bounded fail-open) |
| **LB3** SUCCESS 写移进 persister | ✅ **sound** | 带 3 门(alpha_id 在 / 非 _robustness_failed / verdict∈PASS,PROV);自开 session;soft-fail 绝不丢 alpha persist;drop hge_level>=2 guard |
| **LB4** reward+PROMOTE 改 orthogonal-yield | ❌ **flawed → 驳回** | 见 §6。保持 binary;orthogonal 只作二级 tie-break + min-positive-class + 降级-open;OS 数据(~2026-07)前不能宣称 killswitch 守护 |
| **LB5** retry/mutate 作队列行 | risky-needs-guard | mutate depth 走现成 Hypothesis.r1b_mutation_depth 链,只 retry 用 context 计数器;FIFO 无优先级→retry 挤新发现槽→必须 canary 单区 + shadow-first + yield-kill(非 raw PASS);gate 在 L2 归因 >~15% implementation-FAIL |
| **LB6** 注入 rag_service 修 L0/L3 | overstated(前提错) | rag_service **已构造注入、读路径 live**;config 注入 no-op。L0/L2/L3 暗是**时序**;要 firing 得 codegen 后**第二跳** retrieval(传 current_expression),带 token-budget + idle-in-txn(re-rollback)守卫 + **新专用 flag**(非复用 ENABLE_RAG_CATEGORY_AB,其 control 臂不关 L0/L2/L3) |
| **LB7** 两表分离 + 填桥 | risky-needs-guard | PROMOTE/refresh 是 round-free JOIN 可 beat 驱动(Phase A 纯加性);**ABANDON 不免费**(两 abandon 路都 round-keyed)→ Phase B 新 round-less 判据(**N 连续 0-can_submit 窗口非 0-PASS**,因 67 积压 = 0-PASS 是错信号),单独 flag,守住别 prune 还在产的 cell |

---

## 5. ⚠️ §6 关键修正:submit-gate 深度被数据证伪(LB4 详)

我 v1 把"submit-gate orthogonal-yield reward"当成 execution-limited 下最高杠杆深度。**对抗审查实查 live Postgres 推翻了它**:

1. **早试过、因稀疏 rolled back**:`dataset_weight_refresh.py:15-33` 文档记 reward v1 = (can_submit AND marginal>0) → "~20 命中→太稀→posterior 塌到纯探索 floor→steer 到已证弱的数据集" → 换成 binary v6。LB4 的 AND-门是它**更稀疏的兄弟**。
2. **实查稀疏致命**:AIAC 史上 can_submit=18 → 非 SKIP 只 2 → 加 self_corr<0.7 **全门只剩 1 个 alpha(全历史)**。30d 窗口(bandit 折扣窗)2216 real sim → 1 个全门 → reward ~0 正例 → **posterior 必崩**(重现 v1)。
3. **PROMOTE 塌缩**:orthogonal-yield 把可 promote 假设从 ~8(binary)砍到 **1** → 冻结生命周期。
4. **killswitch 休眠**:marginal_recon 需 ≥15 对(offline ΔSharpe, realized OS sharpe);live 13 submitted **0 个 os_sharpe**(~2026-07 才有)→ verdict 恒 insufficient_sample → **永不 fire** → 所谓"fallback to binary"永不触发 → orthogonal proxy 零验证运行。

**结论:** reward 和 auto_promote **保持 binary can_submit**(刻意的 v6 选择)。orthogonal-yield **只能**作:already-binary-positive 臂内的二级 tie-break排序 + 要求 positive class ≥~10/window 才激活 + 否则降级-open binary。提交吞吐瓶颈是 auto_submit/marginal_drain 的活(LB4 自承 complementary),**不该用一个数据证伪的 discovery-reward 改动去付**。

---

## 6. 数据模型(零迁移为主)

- **认知脊柱零迁移**:`candidate_queue.current_hypothesis_id`(FK→hypotheses)+ `alphas/alpha_failures.hypothesis_id` 全已存在、全已接(workers.py:407 投影,persister.py:115/127/143 写)→ 删 hge_level gate 即填充,**零 schema 改**。
- `candidate_queue.context`(JSONB,存在):加 `cognitive_depth`(retry,Phase 2)+ `parent_*` breadcrumb。无迁移。
- `alphas.metrics`(JSONB,存在):加 `_submit_yield_label` 子对象(仿 `_iqc_marginal`)。无迁移。
- `knowledge_entries.meta_data`(JSONB,存在):SUCCESS/FAILURE 写时加 hypothesis_id + submit-gate 二级标签。`compute_pattern_hash` **冻结**(全非 key)。`none_as_null=True`。
- `hyp_intent.config_snapshot`(JSONB):加可选 `seed_parent_hypothesis_id`(Phase 2 mutate)。无迁移。
- **唯一小加列 Alembic(Phase 1c,server_default 安全)**:`hypotheses.can_submit_count INT DEFAULT 0` + `submitted_count INT DEFAULT 0`(denorm,驱动 promote/abandon)。可选 `attribution String(20)`。
- Watermark:1 个 Redis key `pool:reconcile:last_done_id`(beat 只扫近窗)。无 schema。幂等可重扫。
- `HypothesisRoundStats`:**deprecate 不删**(留 legacy 读;drop 推迟)。
- config:`ENABLE_POOL_COGNITIVE_RECONCILE`(默认 OFF)+ `DATASET_BANDIT_ORTHOGONAL_REWARD`(默认 OFF,仅 §5 二级)+ beat_schedule 项。

---

## 7. 分阶段实施(每阶段 INERT/DARK + gate)

**Phase 1a — 接 FK 脊柱(INERT,无行为变更,无迁移)**
删 hge_level gate;lift generation.py:832-930 在 HG 无条件跑(保 soft-fail try/except;只 lift INSERT,删 V-22.13 739-812;加 intent dedup key)。`current_hypothesis_id` 每候选非 NULL,已接的 persister 开始写 alpha.hypothesis_id。
**Gate:** 24-48h soak,alpha/日持平,hypotheses 行累积,current_hypothesis_id 非 NULL 率→100%。
> ⚠️ LB1:**1a 不能单发** —— 同 Phase 必须有消费者(否则僵尸 PROPOSED),故 1a 与 1c 的 beat **打包**或紧随。

**Phase 1b — 闭合 KB 写对称(Tactical,无生命周期)**
把 record_success_pattern 从死 node_save_results 抽进 persister 每-alpha 循环,带 3 门(alpha_id/非 robustness_failed/PASS|PROV),soft-fail,自开 session。SUCCESS_PATTERN 重新增长。stamp meta_data.hypothesis_id。
**Gate:** SUCCESS_PATTERN 计数回升,零 alpha-persist 因 KB 写失败被丢,F1 单 session 不变。
> LB3 是唯一全过对抗审查的决策 → **可最先独立 ship**(不依赖 1a)。

**Phase 1c — 加列迁移 + 认知 beat(SHIP DARK)**
Alembic:hypotheses.can_submit_count/submitted_count。建 `run_pool_cognitive_reconcile`(gate `ENABLE_POOL_COGNITIVE_RECONCILE`=OFF,注册 beat,fire 但 no-op)。它**按 watermark 只扫近窗** candidate_queue(DONE)+ alphas/failures JOIN hypothesis_id,驱动 auto_activate + refresh_stats(PROMOTE Phase A)+ 归因(early_stop.classify_attribution)。删现已安全孤儿的 in-graph 死路(node_save_results / _process_hypothesis_feedback round-keying / in-graph r1b 边)。
**Gate:** shadow 翻 ON,验生命周期 transition fire、watermark 单调推进、重扫幂等(无双 promote)、无 idle-in-txn。**PROMOTE 判据先别用 pass_count>0**(会 promote on PROVISIONAL)。

**Phase 1d — submit-gate 仪表(SHIP DARK,非 reward 改动)**
扩 refresh_can_submit_for_alpha 算 + stamp `_submit_yield_label`(marginal + self_corr_vs_pool + margin_bps)——dense label,可立即 backfill 67 积压。**这是仪表,不是 §5 驳回的 reward 改动。** ABANDON(Phase B,round-less,N 连续 0-can_submit,单独 flag)。
**Gate:** backfill 67 行;观察 backlog 燃尽 + per-dataset orthogonal-yield(**非 PASS 率**)。orthogonal reward **不在此启用**(§5)。

**Phase 2 — 自进化(数据驱动 gate)**
- retry/mutate 作队列行:reconcile-beat INSERT 新 candidate(retry,depth+1,context 计数器)/ hyp_intent(mutate,seed_parent,depth 走 Hypothesis 链)。**gate 在 1c 归因数据显示 implementation-FAIL >~15%** + canary 单区 + yield-kill(非 raw PASS)+ 预算化(别挤新发现)。
- 表达式感知 RAG(L0/L2/L3):codegen 后**第二跳** retrieval 传 current_expression,带 token-budget + idle-in-txn re-rollback 守卫 + **新专用 flag**。先 A/B 证 lift 再 flip(先验:只 L1 firing 过、pillar 从不动 PASS)。
- 归因升级:classify_attribution_llm 聚合 per-hypothesis(LLM 成本可接受,离热路径)。

**每阶段独立可逆**(flag OFF 或 revert 单改动);live ~207 alpha/日的池从不被未证实的认知改动 gate。

---

## 8. 风险 / 守卫清单(对抗审查汇总)

1. **1a 不能单发**(LB1):无消费者 beat → 僵尸 PROPOSED(重现 V-22.13 "0 abandoned")。
2. **只 lift INSERT 块,别 un-gate V-22.13**(LB1):否则 hot path 每 HG 调多开 get_by_id session。
3. **intent dedup key**(LB1):lease-recycle 的 HG 重跑会在 emit 前 commit 重复 hypothesis 行 → meta_data 塞 intent_id dedup(hash-frozen 安全)。
4. **beat 按 watermark 扫近窗**(LB2):非 refresh_all_stats 全 active → 否则随 hypotheses 累积线性变慢。
5. **auto_promote 别用 pass_count>0**(LB1/LB2):会 promote on PASS_PROVISIONAL(refresh_stats:717 把 PROV 算 pass)。
6. **SUCCESS 写带 3 门、非字面"无条件"**(LB3)。
7. **orthogonal-yield reward 数据死,保持 binary**(LB4):§5。
8. **retry 烧 K_S=2 槽挤新发现**(LB5):canary + shadow + yield-kill + 预算,gate 在归因数据。
9. **rag_service 已 live,L0/L3 是时序**(LB6):要第二跳 + 专用 flag,别复用 AB flag。
10. **ABANDON 用 0-can_submit 窗口非 0-PASS**(LB7):67 积压下 0-PASS 是错信号;守住别 prune 在产 cell。
11. **单测盲区**(workflow①):`test_pool_hg.py` mock 硬编码 current_hypothesis_id=5 掩盖 0/2442 死 FK → 修 LB1 须配真实 config-gate 测。
