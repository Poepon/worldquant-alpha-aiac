# KB / 接线 / 反馈环 —— 池原生重设计 (Pool Phase 2)

> 2026-06-06 起 · v3(三 workflow + 两轮对抗证伪 + 业界案例并入,2026-06-07)
> 方法:live Postgres 实查 + workflow①(`wy32yozh5`,映射池化后真实接线 + ROI gap + 对抗证伪)+ workflow②(`w2ecldgxe`,3 视角池原生设计 → 综合 → 7 承重决策对抗证伪)+ workflow③(`wrm1t4ic4`,5 轴业界/学术案例调研 + 适用性对抗 critique,见 §9)+ 人工核验承重根因。
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

> **v3 修订裁决(workflow④ `wn9mnw7ws`,4 critic 对 live 代码压力测试)= TARGETED 修订,非推倒重设计。** 骨架(1b → 1a+1c → 1d → Phase 2 gated)全部存活、确认 sound + 非回归。但 §9 引出的「anti-crowding 提到 Phase 1 作最高 ROI 新轨」被**三条 live 证据驳回**(§7.0),只存活一个小得多、诚实定范围的切片。

### 7.0 被驳回的再排序(institutional memory — 别再第三次重 derive)
「anti-crowding 禁用表 = 单点最高 ROI、hook 已存在」三条独立证据驳回:
1. **hook 假**:`recent_dedup_skeletons` 在池生成路 **0 读**(state.py FIFO,post-sim 写,只被退役 FLAT 的 `t1_strategy_select`/`tier_seed` 消费,池 `_build_generation_graph`〔workflow.py:257〕从不接);唯一 live 黑名单槽 `avoid_patterns` 只吃 `strategy_dict`,池 hg_run_config 无 'strategy' 键 → 恒 `[]`。
2. **已 live、且故意更粗**:anti-crowding 已由 `OrthogonalitySteeringEnricher`→`submitted_pool_profile`(generation.py:494/536,`ENABLE_ORTHOGONAL_PROMPT_STEERING` ON since 2026-06-05)在**经济-pillar 粒度**实现;`docs/orthogonality_steered_exploration_plan_2026-06-05.md §6` **明确弃用 skeleton 粒度**(survivor-bias)+ 选 soft-nudge 非 hard-filter。R1a 在重 derive 一个被推翻的设计。
3. **数据驳 + C2 未证**:top skeleton 116/4000 同时是 #1「crowded」+ 13 提交赢家之一 + backlog 5× → submitted-pool-keyed 禁用表会 **ban 掉已证过门配方**;13 提交 = 13 个**不同** skeleton(零池内 crowding);真正杀的门 self_corr<0.7 是 **PnL-序列级非 skeleton-字符串级**。C2 转化(更正交生成→更多 SUBMITTED)是 orthogonality 计划 §1.2 自承的**核心未测 A/B**,非已知。
→ **正确下一步:让已 live 的 pillar nudge 累积 orthogonality_score 分布、跑它既定 A/B,再决定要不要加新生成先验轨。**

### Phase 1 — 4 条并行轨

**Track A(最先,完全独立):1b** —— `record_success_pattern` 移进池 E-persister(LB3,唯一全 sound)。RAG 已读 SUCCESS_PATTERN(1596 行,注入 code_gen generation.py:1037/1095)但池从不建。纯写对称,不碰生命周期,无迁移。是 R1b/AWM 的语料前置。
**Gate:** `--all` + 回归 0 漂移;live 池一轮后 SUCCESS_PATTERN 行数升;code_gen `[:5]` 注入字节不变。

**Track B(并行 A,KB+prompt 侧只读):R1a-v1 ONLY** —— frequency-based **SOFT 降权**(非硬禁用表),从 `expression_to_skeleton` 挖近窗 KB,**re-anchor 到 live 注入点**(node_rag_query→build_hypothesis_prompt generation.py:520-563),非死的 recent_dedup_skeletons。必须:soft、sample-size-gated、field-aware(复用 portfolio_skeletons 两因子 skeleton+fields+params±20%)、复用 `[:5]` cap、**新专用 flag**(非复用 RAG_CATEGORY_AB)。**丢弃**:hook-exists 前提 / 最高-ROI 宣称 / hard-forbidden / submitted-pool keying(→R1a-v2 gated)。R1b/AWM **不在 Phase 1**(demote Phase 2 prompt-format tweak,gated on 1b)。
**Gate:** soak 时 HG code_gen **token 截断遥测钉 0**(镜像 `6d303b1` schema 膨胀);throughput 持平**且 skeleton 多样性分布不收窄**(breadth-collapse = 隐形回归);flag 默认 OFF 直到 soak 过 + **live pillar nudge 自己的 A/B 报告前不升级**。

**Track C(1a+1c 打包,R3 作硬实现约束):**
1a:删 hge_level gate,HG 无条件建 hypotheses 行(只 lift INSERT 832-930,删 V-22.13 739-812,加 intent dedup key;FK 0/2442→填充,零迁移);**必须和 1c 同发**(LB1:单发→僵尸 PROPOSED)。
1c:新异步 `run_pool_cognitive_reconcile` beat(今确缺,gate `ENABLE_POOL_COGNITIVE_RECONCILE`=OFF),驱动 auto_activate/refresh_stats/PROMOTE + 廉价归因(early_stop.classify_attribution)。删现安全孤儿的 in-graph 死路(node_save_results / _process_hypothesis_feedback round-keying / in-graph r1b 边)。
**R3 纪律 baked in(非 Phase 2):**(i) **watermark on `alphas.created_at` + GRACE PERIOD**(≥2× 30s can_submit refresh 倒计时),复用 `cognitive_layer_bandit_tasks.py:80-85`/`dataset_weight_refresh.py:138-141` 既证幂等模式 —— **丢弃「双时态 point-in-time JOIN」黑话**(无 sim-landed 列:candidate_queue.updated_at 每跳 bump、alphas dual-tz);真要 point-in-time correctness 才加一个 `label_landed_at` 列。(ii) 幂等 watermark upsert = 每 hypothesis_id 每窗一写(atomic emit 已在 workers.py:453-485,别承诺修 E-persist 多 commit 残留)。(iii) **censored-not-negative**:can_submit IS NULL → CENSORED(排除、不扣 pull),绝不 beta=0。**PROMOTE 判据先别用 pass_count>0**(会 promote on PROVISIONAL)。
**Gate:** beat 离热路径(只读 DONE 行,K_E=1 不碰,LB2);幂等重跑 stats 一致;1a+1c 同落后僵尸 PROPOSED=0;grace 经验证(新落 alpha 在 can_submit 标签落前不被扫)。

**Track D(1d dark + bandit censoring 回填):**
1d:扩 refresh_can_submit_for_alpha stamp `_submit_yield_label`(marginal/self_corr_vs_pool/margin_bps)进 alpha.metrics —— **仅仪表**(LB4:reward 保持 binary)。可立即 backfill 67 积压。
**PLUS 真正可立即 ship 的修正(R3 揪出的 LIVE bug):** `dataset_weight_refresh._classify`(:82-90)今天对 NULL can_submit 返 `(real=True, reward=0.0)`,把 67 backlog + 在飞 sim 折进 beta=0 作真 pull,**正在腐蚀 7 天窗 dataset bandit**。修:NULL/未刷=censored(drop、不扣 pull)。**独立于整个重设计,应作独立热修先上。**
**重锚:** `compute_max_corr_vs_pool` 已删(`b53b65a`)→ 全部引用改 `calc_self_corr`(correlation_service.py:389,候选 PnL vs OS 池,PnL 未就绪返 UNKNOWN)。
**Gate:** `_submit_yield_label` 出现在新刷 alpha;dataset-bandit 对 NULL-can_submit 行停止 incr T_d(纯 `_classify` 单测断言);binary reward 不变。

### Phase 1 TAIL(gated on 1d 标签落地,acyclic 非同相)
**R2(收窄)→ R1a-v2。** R2 第三归因桶**收窄到 self_corr ONLY**(eval-time evaluation.py:891-896 `_self_corr` / :714 orthogonality_score):「PASS-band 独立指标 ∧ `_self_corr`≥max → skeleton 多样性惩罚」。**排除 marginal≥5bps**(归因时不可得,只 post-can_submit async IQC refresh_tasks.py:386-395,且 recon killswitch 可能死 proxy);marginal 留 1d 仪表。第三桶→惩罚边**必须 count-thresholded reconcile 聚合**(K≥3-5 个 hash 级 self_corr-fail 才惩罚)+ **跳过 `_self_corr` UNKNOWN 行**(PnL 未就绪 evaluation.py:706-715)→ 防 n=1 早禁。然后 R1a-v2:把 v1 频率表升级成 crowding-weighted,读 1d 的 self_corr_vs_pool。3 跳链(v1 频率[P1] → 1d 标签+R2[tail] → v2 crowding)解 R1a↔R2 鸡生蛋。
**Gate:** R2 只在 K≥3-5 hash 级 fail 触发;UNKNOWN 排除;R1a-v2 只读已落 1d 标签(HG 路无即时 PnL pull);多样性持续监控。

### Phase 2(数据 gated)
R5(**DIFF/targeted-edit retry 非从头重生**)作新队列行 + success-gated budget(**起始=1、正交感知 trigger 非 raw fitness running-max、canary 单区**)+ LB5 守卫逐字(canary+shadow+yield-kill)—— **唯一碰 K_S=2 sim 槽的项,守卫强制**。+ 表达式感知 RAG 第二跳(传 current_expression,token-budget + idle-in-txn re-rollback 守卫 + 新专用 flag,先 A/B 证 lift)+ LLM 归因升级(classify_attribution_llm —— **归因-TEXT 语料今天不存在**,只 4 值 heuristic enum,在此 bootstrap,C4 下数月)。R1b/AWM 可选 refinement 落这,gated on 1b 语料。
**Gate:** R5 canary 单区;sim 槽竞争遥测(K_S=2 不饿);正交感知 trigger;yield-kill if retry 转 SUBMITTED-orthogonal 不超 fresh baseline。

### DEFERRED past Phase 2(不排期)
**R4 GEPA/DSPy 离线 prompt 编译器** —— 3 个未建前置(reconcile-beat 缺、归因-TEXT 语料缺、**prompt-毕业 seam 缺**:HG 用 module 级 Python 字符串常量 generation.py:51-57,无 prompt_version/compiled_prompt,毕业一个 prompt=改源+重启)+ DSPy/GEPA 未装 + **目标错配**(唯一便宜离线目标是 P(PASS)/多样性,但 C2 说更多 PASS 没用;对的目标 orthogonal-yield 正是被 3× 证死的)。若将来做,首 PR = DB/flag-overridable prompt-resolution seam(镜像 _flag_override_cache 热刷)。

**每阶段独立可逆**(flag OFF 或 revert 单改动);live ~207 alpha/日的池从不被未证认知改动 gate。

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
12. **breadth-collapse 是最坏隐形回归**(workflow④):即便 soft R1a-v1 也可能在 throughput 持平下静默收窄生成到更少 cell → **必须监控 skeleton-多样性分布非只 throughput**;live pillar nudge 已带此险,加第二个频率先验复合 → R1a-v1 默认 OFF 直到自己 soak + pillar A/B 双过。
13. **can_submit 标签滞后 censoring bug 今天 LIVE**(workflow④,可独立热修):`dataset_weight_refresh.py:82-90` 对 NULL can_submit 返 reward=0 作真 pull,持续腐蚀 7 天窗 dataset bandit;独立于整个 KB 重设计,1d 的 censored-not-negative 应作**独立热修先上**(并复核 cognitive bandit `:97-100` 的 accidental-correct drop 没静默丢真信号)。
14. **C2 转化仍未证**(workflow④,决定性):没有任何东西证明更正交生成 → 更多 SUBMITTED(67 vs 13);决定性实验 = 已 live 的 pillar nudge **既定 A/B** → 先跑它,若 steering 不动 submit-yield,R1a-v1/v2/AWM 全失去理由不该 ship。
15. **R1a-v2/R2 硬依赖 1d backfill 完整性 + grace 经验导出**(workflow④):部分 backfill → crowding 信号偏样本 → R1a-v2 gate 在 backfill-completeness check 非只 1d 代码 ship;reconcile grace 上界须从观测 p95 标签落地延迟导出非硬编码,censored-not-negative 作 grace 失调兜底。

---

## 9. 业界 / 学术案例参考(并入,源 workflow③ `wrm1t4ic4` 2026-06-07)

**TL;DR:** 文献干净分两营,**没有一营住在我们的交点上**——**Camp A(自进化 agent + LLM 量化:Voyager/Reflexion/ExpeL/CoSTEER/QuantaAlpha/AlphaAgent/AlphaGen/RD-Agent(Q))全假设稠密、per-trial、可观测奖励** → 借**认知形状**;**Camp B(延迟标签生产:Criteo/延迟转化 bandit/SSRS/PU 学习/watermark 流)给对了异步 plumbing,但都最终积累够真标签**。我们的 C1(黑盒无梯度无 realized PnL)+ C2(execution-limited)+ C4(~1 lifetime 正例)落两营之间。**从 A 借形状、从 B 借 plumbing,但 orthogonality-primary promotion + self-policing-proxy + ≥5bps 经济门是净新无先例。** 用法铁律:先例分"能借(plumbing/生成先验/离线)"vs"浪漫化(假设我们没有的稠密奖励/可观测 PnL/便宜搜索)",后者**按数据驳回非仅 caveat**。

### 9.1 最像我们的 3 个
- **Algoplexity「AZR + R&D-Agent(Q) on WorldQuant BRAIN」**(博客提案,algoplexity.github.io/cybernetic-intelligence/Brain/)— **唯一对标我们确切平台 + 把 BRAIN 当 metric-only 黑盒**。拓扑最近,但博客提案非生产 → **借作"验证我们押注对了"非借机制**;它自己点名我们正设计的 3 坑(① 在脱钩提交结果的 simulator reward 上学=我们 67-vs-13 / ② 只 replay buffer 无 pattern KB+谱系=D1/D2 缺 / ③ 对 LIVE 池去相关欠规范=marginal_recon 答)。
- **RD-Agent(Q)/CoSTEER**(微软 arxiv 2505.15155,开源)— 我们范式本体,**认知层最近**(SOTA-set=D2 / 持久 H_t,F_t 集=D1 / (task,code,feedback) 三元相似检索=skeleton-keyed RAG / contextual-Thompson bandit=D5 祖先 / complexity-bump=D4 预算)。**分歧**:稠密可观测回测奖励(非黑盒门)、缺 D3 显式归因、无正交/执行约束。
- **QuantaAlpha(arxiv 2602.07085)+ AlphaAgent(arxiv 2502.16789)**— **per-candidate 进化 + 归因最近**。QuantaAlpha 三层重写(hypothesis/expression/code)=最干净 D3;`\|corr\|<0.7` 池准入**字面=我们 BRAIN 门**。AlphaAgent hypothesis-misalignment-vs-structural-violation=**可直接抄的 D3 分类法**,alpha-decay/crowding 最接近 C2 —— **但其 anti-crowding 是对固定 Alpha101 zoo 的静态 AST,我们是 LIVE 演化同区 SUBMITTED 池**。

### 9.2 决策映射 D1–D5

| 决策 | 最佳先例 | 借什么 | 怎么不同(我们约束) |
|---|---|---|---|
| **D1** Pattern KB | Voyager verify-before-store(2305.16291)/ ExpeL(2308.10144)/ **AWM induction**(2409.07429)/ Generative Agents(2304.03442)/ A-Mem / Mem0 | verify-before-store(过 band 才写)/ AWM **skeleton 带槽模板** / 重要性用真 sharpe 非 LLM 自报 | C1:"success"=in-sample band+正交非 submit gate;C4:从 band winners 诱导**绝不从 gate-clearer**(仅 1 个);组合性 break→只作生成先验 |
| **D2** 假设生命周期 | CoSTEER SOTA-set+H_t/F_t / FunSearch islands(nature s41586-023-06924-6)/ 特征库 point-in-time JOIN | SOTA-set=PROMOTED 参考集种 HG;事件驱动触发;**双时态 point-in-time-correct JOIN** | C1:promote key band+正交,真 gate 作稀疏离线审计;**C2 无先例**:须加 marginal/self-corr-headroom 到 promote;拓扑 DB 行非内存树,beat 须 re-read |
| **D3** 归因 | **AlphaAgent 两桶(2502.16789)可直接抄** / QuantaAlpha 三层(2602.07085)/ Reflexion(2303.11366)/ Alpha-Jungle weakest-dim(2505.11122) | 两桶(实现失败→retry / 假设失败→生命周期)/ weakest-dimension→targeted edit / **存为 TEXT**(=TextGrad/GEPA 梯度) | C1:别人有稠密 OOS IC 确认,我们 in-sample→"假设失败"provisional;**须加第三桶:好 alpha 过不了正交门→多样性惩罚=净新** |
| **D4** 自进化 | **Alpha-Jungle success-gated budget**(2505.11122)/ AlphaEvolve+OpenEvolve(2506.13131)/ QuantaAlpha localize-rewrite / TextGrad(2406.07496)/ Deflated-Sharpe | success-gated 预算 / **DIFF 编辑非重生** / **slot 前 cascade** / depth 双作多重检验上限 / 偏 mutation 非 crossover | C3 主导:别人 10^6 eval,我们 ~3 槽→cascade 任务是**最小化到达昂贵阶段**;C2:只超 fitness 不超正交的 child 不准 promote;进化 fitness 用稠密 proxy 绝不 submit gate |
| **D5** 战略 reward | TLRS 稀疏诊断(2507.20263)/ SSRS(2501.19128)/ **延迟转化 bandit censored-not-negative**(1706.09186)/ Criteo(KDD'14)/ **PU 学习**(2303.08269) | reward 绑**稠密 proxy 绝不 gate** / **censored-not-negative**(67 积压是未标注正例)/ **SCAR 违反**(submitted 人选非随机) | **C4 致命**:别人最终拿够真标签估率,我们 ~1 正例→真标签 posterior 退化→**必须绑稠密 proxy**;C1:无 IC 锚→不能 LEARN surrogate 只能手工构造+离线 sign-check |

### 9.3 ✅ 安全可借(critique 验过,大多 plumbing/生成先验/离线,不依赖稠密奖励)
1. **D1 verify-before-store(Voyager)= 我们 LB3**(唯一全过):过 band 才写 SUCCESS_PATTERN。文献 #1 反模式="Reflexion-without-Voyager"(只记失败)= 我们现状。是 plumbing 非理论。
2. **AWM skeleton-induction 作纯生成先验**:band winners 聚带槽模板喂 HG prompt。免费 DB 聚合。
3. **生成期 anti-crowding(Frequent-Subtree-Avoidance + AST-originality)**:KB 挖最常用 skeleton 注入 HG prompt 作禁用表。零 sim,服务 C2/C3。**对 LIVE 同区 SUBMITTED 池测不是固定 zoo**。
4. **D3 AlphaAgent 两桶 + QuantaAlpha 三层,存 TEXT**:大多已建(attribution_types.py/early_stop)。**+ 第三桶(好 alpha 过不了正交门→多样性惩罚)= 净新**。
5. **D3 便宜 pre-sim alignment check C(h,d,f)**:占 sim 槽前拒 hypothesis-misaligned=C3 最高杠杆纯赢。
6. **D4 forward-only DB lineage + DIFF retry + 偏 mutation 非 crossover**:拓扑=我们池;crossover 退役我们 MEMORY+GP 文献都证。
7. **reconcile-beat 工程成 delayed-label-join 管道**:transactional-outbox 单写每跳/幂等 upsert/watermark+grace/**双时态 point-in-time-correct JOIN**。镜像 orthogonality_score clobber 教训 + 满足 LB2 守卫。
8. **censored-not-negative 标签纪律(延迟转化 bandit + PU)**:67 积压=未标注正例非失败,别折进 beta_param=0;SCAR→proxy 偏人口味→**保持 bandit 简单,别训神经 reward head**。
9. **离线 GEPA/DSPy prompt 编译器 FROM reconcile-beat**(GEPA 2507.19457/DSPy):历史语料 replay,**零新 BRAIN sim**(绕开 C3/C4),只离线验过 prompt 上 live worker。**最干净 fit,prompt 优化正确家**(不在 per-candidate 环)。

### 9.4 ❌ 被驳回/浪漫化(与 §5/LB4 一致 —— 第三次独立撞同一结论)
1. **❌❌ D5 orthogonality-surrogate reward**(synthesizer 最大 overclaim)— critique 判**"被我们 live 数据证伪非仅浪漫化"**:就是已 rolled back 的 v1 的更稀疏版,30d 窗 ~0 正例必崩。**保持 binary can_submit**(§5)。
2. **❌ marginal_recon kill-switch 当"已有安全网"反了**:休眠(需 ≥15 OS 对现 0,before-and-after 对已提交返 400,~2026-07 才有)→ 永 insufficient_sample → 守不住。C1 结构性不可得非延迟。
3. **⚠️ FunSearch island-cull + CoSTEER SOTA-set 统计**:假设 10^6 便宜确定性 eval(C3 违反);只借节奏/形状,abandon 自己发明 round-less(N 连续 0-can_submit 非 0-PASS,=LB7)。
4. **⚠️ ExpeL contrastive-LLM + AlphaGen/RD-Agent IC/contextual reward**:偷藏稠密可观测奖励(C1)+ LLM 调用挤 HG 预算;bandit 保持简单 Beta-Bernoulli 非 contextual/neural(几十事件过拟合)。
5. **⚠️ Alpha-Jungle 预算=3 起**:只在 trigger 正交感知(非 raw running-max)+ 起始预算=1 + canary gated 时才活(retry FIFO 下饿死新发现,=LB5)。

### 9.5 我们真正 on-our-own 的 3 个净新负担(无先例)
1. **ORTHOGONALITY-AS-PRIMARY(C2)**:所有系统优化独立质量、crowding 当对固定 zoo 静态后过滤;我们 LIVE 演化同区 SUBMITTED 池。"更多 PASS 没用、更多可提交-正交才有用"无类比 → 自己发明 marginal-正交 promote 判据(D2)+ 第三 D3 桶。
2. **PROXY-WITHOUT-ANCHOR(C1+C4)**:别人朝**已知**稠密目标 shape proxy;我们朝基本永远观测不到的目标 shape,proxy 唯一有效性检查=离线 sign-agreement kill-switch(小、间歇可得探针)。自我监管 proxy(marginal_recon)我们自建无先例。
3. **≥5 bps 经济-margin 门** 叠在统计正交上 —— 真实交易成本约束,量化挖掘论文(止于 IC/IR)从不建模。

### 9.6 借鉴 → Phase/LB 衔接
| 借鉴 | 落点 |
|---|---|
| verify-before-store(Voyager)| **LB3 / Phase 1b**(唯一 sound)|
| 归因两桶+三层 + alignment 预门 | **LB2 Phase 1c shadow 归因** + D3 第三桶(净新)|
| reconcile-beat=delayed-label-join plumbing | **LB2 Phase 1c**(满足 watermark 守卫)|
| censored-not-negative + 保持 binary reward | **LB4 修正 / §5**(数据驳回 orthogonal reward)|
| AWM skeleton-induction + anti-crowding 禁用表 | **战术层 D1 / Phase 1b-2 生成先验**(零 sim)|
| forward-only lineage + DIFF retry + mutation>crossover + success-gated budget(预算=1+正交 trigger+canary)| **LB5 / Phase 2**(数据 gated)|
| 离线 GEPA/DSPy prompt 编译器 | **新 Phase 2 候选**(零 sim,beat 跑历史语料,最干净)|

**一句话:业界给了认知形状(camp A)+ 异步 plumbing(camp B)两半,但绑到黑盒+execution-limited+~1-正例上、以及 orthogonality-primary / self-policing-proxy / ≥5bps 经济门,是我们必须自己造的 —— 无现成案例,且任何想把稀有真目标做成频繁 firing 学习环的尝试(如 D5 reward 升级)必须按数据驳回。**
