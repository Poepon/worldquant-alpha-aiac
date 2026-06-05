# 正交导向自动探索（Orthogonality-Steered Exploration）设计 plan **v4**

> v1 生成 2026-06-05 · v2 = 应用 3-镜头 fresh-agent review(2× GO_WITH_FIXES + 1× REVISE)全部 fix · v3 = 2-镜头收敛复审(两镜头均 GO_WITH_MINOR = 收敛)后应用 MUST:①反 gaming 护栏(§4)②OS PnL 新鲜度升硬 gate(§5-9)③SD 是假设须 shadow 验 ④infer_pillar momentum-46% ship 前人工审(§5-12)⑤orthogonality_score 接线为第一 build 步(§3)。
> **v4 = orthogonality_score 指标重设计(shadow 实跑暴露 v3 接线 DOA)**。shadow run(任务 3964/3981/3982)记 0 个 orthogonality_score,根因两条:(a) v3 用 `1 − self_corr`,而 `get_with_fallback` 对**新挖候选恒 UNKNOWN**(本地缓存无其 PnL + BRAIN SELF 异步 PENDING)→ fresh 候选永无值;(b) 即便有值也会被 `evaluation.py:895` 的 `alpha.metrics = {**metrics, …}` 重建**覆盖**(原 ba65ac7 写法 reassign 一个 alpha.metrics 副本,从未进 895 的 spread)。**v4 修复**:新增 `CorrelationService.compute_max_corr_vs_pool(alpha_id, region)` —— post-sim **拉候选 PnL 一次**(per-round 缓存)再 vs 已提交池逐列算 `max|Pearson|`,`orthogonality_score = 1 − max_corr`,**对 fresh 候选可算**;写进 `metrics` 局部(被 895 spread,真落库);**flag-gated + 仅 sharpe≥sharpe_min 候选**(省成本 + 反 gaming)。详见 §3/§4。**plan 收敛,Phase A 指标可真跑。**
> 起源:用户「当务之急是挖掘新 alpha」→「应该 LLM 自动探索正交源」。映射 4 子系统收敛:正交信号已存在且可算,但与挖掘环脱节。
> **用户决策(2026-06-05)**:范围 A+B / 接受 sim 成本 / 现在做 / **粒度改用既有 pillar(review 推翻 v1 的 mechanism_family,见 §6)** / 推进 Phase A。
> 状态:**Phase A 代码已建(foundation + 热路径 + v4 指标重设计),全 flag-OFF 字节等价;待 flag-ON shadow 跑出非零 orthogonality_score 再进 A/B。** 未改 plan 外行为前本 plan 为准。

---

## 0. 一句话
把「与已提交池的正交度」从**事后提交门**变成**前置探索目标** —— 让 LLM 生成主动往「与已提交池正交」的方向探索,使挖的每个 alpha 更可能是正交新货,而非又一个 pv1 相关货。

---

## 1. 背景 / 边界(诚实)

### 1.1 数据现实(2026-06-05 live PG)
- delay-1 共 2199 alpha,**can_submit 19 里 17 来自 pv1**(价量);其余 16 数据集各挖 45-240 个,can_submit ≈ 0-1。
- 提交门 = self_corr < 0.7 vs 已提交池(同 region)。再挖 pv1→相关撞门;再挖其它→0 货。
- 当前挖掘 **IDLE**(仅 task 3930 PAUSED@6-02)。

### 1.2 解决 / 不解决(review 加固)
- ✅ **解决**:挖掘环对正交性「盲」(只 steer margin+coverage,从不看候选 vs 已提交池相关)。
- ❌ **不解决**:供给天花板。正交导向 = discovery 效率,非凭空造正交产货源。正交数据集本就不产货 → steer 过去得 0 货。真正扩供给 = L1 新源。
- ⚠️ **review 关键澄清(frozen-window ≠ steering validation)**:`marginal_recon` 验证的是**事后排序**(冻结窗 offline ΔSharpe vs BRAIN 符号 ~87.5% supported);**steering 是因果干预**(改 LLM 行为→新 alpha→池组成变→信号在新池上变噪)。所以「冻结窗信号能排序」**不证明**「它能引导生成出真正可提交的正交 alpha」—— 这正是 A/B 要测的核心未知,**不是已知**。
- 📌 USA-only 上界**有界**(可能仅 1-2 个新可提交);本 plan 与 L1 互补,L1 解锁后收益远放大。

### 1.3 与 execution-limited / 抽干积压的关系
本 plan 让挖掘**正交导向**(产可提交供给),不与 execution-limited 冲突。**但它不替代「抽干现有 68 积压」(<1d quick win,auto-submit 已 live)** —— 二者并行:N1 抽干现货 + 本 plan 试产新正交货。内建 STOP:A/B 无效即停转 L1。

---

## 2. 现状映射(4 子系统全 `uses_submitted_pool_orthogonality = no`)

| 子系统 | 当前 steer 在(review 校正) | 关键文件 |
|---|---|---|
| dataset 选择 / bandit | ε-greedy:session 内 **mean margin(float bps,非 binary can_submit)** + 类别/数据集覆盖;Thompson bandit(flag OFF)用 pull-count+经验成功率 | `dataset_selector.py:434-694`、`mining_tasks.py:1108-1120,1261-1263`、`persister.py:150-156`、`selection_strategy.py:206+` |
| diversity / tracker | coverage(session-local)+ margin;novelty vs **30d 全 alpha**(含已提交、**无 date_submitted 过滤**,session 内存) | `diversity_tracker.py:155-549`(241-279) |
| hypothesis / RAG / CoSTEER | 数据集类别 + RAG;HypothesisFeedback 丰富但下游不读(CoSTEER 未真闭) | `agents/core/feedback.py:26-215`、`graph/nodes/generation.py:66-185,394-551`、`hierarchical_rag.py:653-830`、`prompts/hypothesis.py:34-101` |
| self_corr / 正交度 | **仅事后**:提交门(`alpha_service.submit_alpha`)+ 提交排序(`marginal_drain.greedy_orthogonal_order`)+ 自动提交守门(`auto_submit_selector` G3b/G9)。挖掘侧零接线 | `correlation_service.py:448-490`、`marginal_drain.py:pairwise_corr_from_pnl/greedy_orthogonal_order`、`auto_submit_selector.py:37-40` |

**review 证实的载重事实**:① `correlation_service` 三层降级(LOCAL→BRAIN→UNKNOWN),本地缓存算候选 vs 已提交池相关 **= 一次性加载已提交 PnL,无 per-候选 BRAIN**(`:324-383`);② `marginal_drain` 纯函数零 BRAIN,已被 auto_submit_selector 调用;③ `PromptContext`(`prompts/base.py:13-100`)可加字段不破现有测试;④ `evaluation.py:449-476` self_corr 仅作 hard gate、不在生成期 → Phase A prompt 注入与 self_corr 计算解耦;⑤ pillar 已存在于 `metrics['pillar']`,**走 metrics JSONB 免 Alembic**。

### 数据层 caveat
本地 `alpha_pnl`/`os_pnls` 是冻结 OS 回测窗(止 2023-12-29),相关性是**冻结窗 offline 代理**(2.5yr 陈旧,非 live)。marginal_drain 同基础。**开工前核 OS PnL 最近刷新时间**(`refresh_os_correlation_cache` beat);若失鲜,A/B 风险升级。

---

## 3. 设计:三档(A+B 本期建,pillar 粒度;C 条件性)

### Phase A — 负向知识注入(prompt 层,**pillar 粒度,LOW 风险**)
**核心**:session 启动算**已提交池的 pillar 覆盖画像**,注入 hypothesis/code_gen prompt 作**软 NUDGE**(非 hard-filter),让 LLM 主动提正交 pillar/字段。

- **数据(用既有 `infer_pillar`,无新分类器、无 Alembic)** — **2026-06-05 真数据验证**:`metrics['pillar']` 全库 0% populated(该 key 从不写;pillar 存 Hypothesis 表),但 `backend.pillar_classifier.infer_pillar(expression=…)` 是**纯函数规则分类器**(operator+field 投票,无 DB/无 LLM)。Phase A session start 拉 `alphas WHERE date_submitted IS NOT NULL AND region=R`,对每个 expression 调 `infer_pillar` → `{pillar: (count, mean_sharpe, high_freq_fields)}`。**纯本地 SQL + 规则分类**,零 BRAIN、零 LLM、零 Alembic。**实测 13 已提交画像**:`momentum 6/13(46% 过度集中,sh 1.68)/ value 2/13(sh 2.25 最高、低挖)/ other 2 / quality·volatility·sentiment 各 1`。→ 注入「过度 momentum,去做正交的 value/quality(高 sharpe 却低挖)」。**关键**:value/quality 高 sharpe 来自 fundamental6(正交且产货面,dataset 级覆盖看不到、pillar 级才显形)→ 印证 Phase A 比既有 dataset-coverage steering 多出真信号。
- **稀疏诚实(review must-fix)**:13/5 ≈ 每 pillar 2-3 个。注入文案**标样本量 + 宽 CI**(「momentum: 4/13, sharpe 1.8±0.4」),`n<2` 的 pillar **不进强 NUDGE**(标「样本不足」),`n=1` 从 NUDGE 移除。避免据小样本过度排斥。**survivor-bias 警示**:13 是「被选出的」非「能选的全部」→ prompt 措辞为「这些已覆盖,**探索其它**」非「这些是好的/坏的」。
- **接线**:`PromptContext.submitted_pool_profile` 新字段;`build_hypothesis_prompt()` 在 **Single-mechanism discipline 段后、investment philosophy 前**插「Portfolio Breadth Principle」块(≤120 token,留 30 安全 margin)。flag OFF 时 `getattr(ctx,'submitted_pool_profile',None)` soft-skip → **字节不变**。
- **telemetry(必须,先于 A/B)**:每 hypothesis 记 ① profile 块实际 token ② 注入了哪些 pillar/字段 ③ 该候选 post-sim 的 `orthogonality_score`(见 §4)④ JSON 解析成功/截断标志。
- **orthogonality_score 接线(v4 重设计,已建)**:`node_evaluate` 的 `_evaluate_single_alpha`(`evaluation.py:~701`):self_corr **MEASURED**(LOCAL/BRAIN)→ `orth = 1 − self_corr`;**UNKNOWN**(fresh 候选常态)+ flag ON + `sharpe ≥ sharpe_min` → 调 `correlation_service.compute_max_corr_vs_pool(alpha_id, region)`(post-sim 拉候选 PnL vs 已提交池 `max|corr|`,per-round PnL 缓存)→ `orth = 1 − max_corr`。**落 `metrics` 局部**(被 `:895` 的 `{**metrics,…}` spread 真持久化 —— v3 的 reassign-alpha.metrics-副本写法会被该重建覆盖,DOA)。flag OFF → 仅 measured 路径 = 与上一 commit 字节等价。`compute_max_corr_vs_pool` 见 `correlation_service.py:324`(7 单测),节点接线见 `test_node_orthogonality_score_fresh_candidate.py`(3 单测:flag ON 记录 / flag OFF 不记录 / sharpe 不过门不调 helper)。**这是 dense 主指标的实现,先于一切 A/B。**
- **ship 前必做审计**:人工核 13 个已提交 expression 的 infer_pillar 赋值(见 §5-12,防 momentum 46% 是归类 artifact)。
- **文件**:`prompts/base.py`(PromptContext 字段)、`prompts/hypothesis.py:34-101`、`prompts/generation.py:1-116`、`graph/nodes/generation.py`(注入点)、新 helper `submitted_pool_profile.py`(聚合,短事务读,遵守 [[reference_flat_idle_in_txn_lock_leak_2026_06_04]])。
- **flag**:`ENABLE_ORTHOGONAL_PROMPT_STEERING`(default OFF,可热翻)。
- **风险**:**LOW**(纯 prompt + 既有 pillar;OFF 字节不变)。**回归测(review must)**:5 个固定 (expr,hypothesis) 对,flag ON/OFF 跑 code_gen,断言 JSON 解析成功 + alphas 数组**顺序无关相等**(防 OFF 时行为漂移)。

### Phase B — novelty vs 已提交池(diversity 层,MED)
- `diversity_tracker.evaluate_diversity()` 加第 6 维 `submitted_pool_distance`。**阈值 = 0.4(非 0.7,review must-fix)**:0.7 是 can_submit 供给门,用它做 novelty = 二值 can_submit 代理(非真 diversity);**0.4 瞄准「与池正交但仍可优化」的研究带**。`distance = 1 − min(max_self_corr_to_submitted/0.4, 1.0)`。
- flag `ENABLE_SUBMITTED_POOL_DIVERSITY`(default OFF)。**接线时点明确**:与 A 同期建(flag OFF 不改行为),**初期仅「记录+audit」不进 overall_score / 选择**;A/B 看「生成 alpha 落 0.4-0.7 带的比例」是否 ↑。
- 文件:`diversity_tracker.py:155-549`、`config.py:1299-1309`。

### Phase C — 边际 ΔSharpe steering + mechanism_family(**gated-on A/B GO**,HIGH)
- bandit `_reward_hook` 从 raw margin 改 marginal ΔSharpe vs 已提交池;`_pick_dataset` 次级排序按 per-dataset corr 代理(daily beat 物化)。
- **mechanism_family 推迟到这里**(v1 的 Q4 诉求):真要分层 RAG(L0=dataset_category, L1=mechanism_family, L2=node)时才加分类器 + 字段。**别在此前把 CoSTEER 复活拽进来**(隐含上游依赖,见 §5)。
- flag `ENABLE_MARGINAL_STEERING_V2`(default OFF)。独立 A/B。

---

## 4. A/B + 验证 gate(**review 重设计:dense 主指标**)

**问题(review 3 镜头收敛 must-fix)**:v1 主指标「正交 can_submit 率」基率 0.86% → N=30/臂 期望 <1 → 检出力 <5%(重蹈 `reference_routing_reasoning_models` n=28 噪声)。

**v2 修复 — 主指标改连续 dense 量**:
- **PRIMARY(shadow + A/B)**:`mean orthogonality_score` per 模拟 alpha = `1 − max_corr_to_submitted_pool`(post-sim 在 node_evaluate 经 `compute_max_corr_vs_pool` 拉候选 PnL vs 已提交池本地缓存算)。**v4 校正密度口径**:仅 **sharpe-pass 候选**有值(flag-gated + sharpe gate,见 §3),非「每个模拟 alpha」——故密度由 sharpe-pass 率决定,N≈30/臂 指 **30 个 sharpe-pass 候选**。次辅:sharpe-pass 候选落 **0.4-0.7 正交带**的比例。⚠ 若 sharpe-pass 率太低致 N 累积慢,shadow 期需相应延长(原 SD 未知问题叠加)。
- **SECONDARY(推迟,需累积)**:二值 `正交 can_submit 率`。当前基率 0.86% → 需 ~400/臂才有力 → **推迟到累积 50+ 可提交后再做**,现在只记录不作 gate。
- **功率**:orthogonality_score 连续量,**假设 SD~0.2 → MDE=+0.08 在 N=30/臂 ~80% power**(`(0.08/(0.2/√30))²≈4.8`)。**⚠ SD 是假设非事实(v2-rereview must)**:shadow 期实测 SD;若 SD≥0.3,N=30 power 跌破 60% → 须延长 A/B 或加大 MDE。
- **🛡 反 gaming 护栏(v2-rereview MUST①)**:orthogonality_score 可被「正交但 sharpe 垃圾」alpha 刷高。所以 **GO 需同时满足**:(1) orthogonality_score 显著 ↑(MDE 达标);(2) mean sharpe 无显著 ↓;(3) **sharpe-pass 率(sharpe≥`EVAL_SHARPE_MIN`)无显著 ↓** ← 防「用正交垃圾换好货」;(4) **interim N40 黄旗**:steered 臂至少比 control 多 ≥1 个「正交带(0.4-0.7)且 sharpe 过门」的 alpha;若 orthogonality_score ↑ 却 0 个正交-且-过门 → 收紧 MDE 到 0.12 再进 N80。
- **流程**:① shadow ≥5d:flag ON 只记 telemetry + counterfactual(含 SD 实测),不改 bandit/选择;**人工复核 SOP**(抽 5-10 steered vs control hypothesis,审注入块是否合理 + **NUDGE 是否真改了 pillar/字段选择**(防软注入是 phlogiston 不起效));② A/B ≥2w:带/不带 steering,**interim N=40 与 N=80/臂**。**interim 决策树**:N40 若 p<0.05 且 Δorth>0.05 → 报告 + 等用户批准早停/继续;N80 若 p<0.05 → 自动执行 GO/STOP gate。
- **STOP gate(双条件)**:`(orthogonality_score steered ≤ control, 无正趋势, Welch t 不显著) AND (正交-且-过门 alpha 率与基线无显著差)` 二者同时 → STOP 转 L1。**单条件不 STOP**。

---

## 5. 坑 / 风险(v2 补 review 项)

1. **正交-但-不产货(最大)**:steer 离 pv1→正交数据集 0 货。缓解:软 NUDGE 非 hard-filter;鼓励「pv1 内部正交新构造」;STOP gate 兜底。
2. **frozen-window ≠ steering validation(review)**:冻结窗信号能排序≠能引导生成。A/B 是验证不是确认。
3. **生成热路径(Phase C)**:gate-on-A/B,default OFF。
4. **prompt 膨胀**:块 ≤120 token + telemetry 实测(shadow 期跑 N=5-10 实测平均大小,非理论估)。
5. **CoSTEER/RAG dormant 债**:Phase A/B **不**依赖激活(走独立 prompt 注入 + 既有 pillar)。**Phase C 的 mechanism_family 分层 RAG 会拖进 CoSTEER 复活** → 隐含上游依赖,C 启动前先评 [[project_rdagent_costeer_loop_closure_2026_05_22]]。
6. **flag hook footgun**:非-ENABLE_ 配置(权重)直读 `_flag_override_cache`(见 [[reference_feature_flag_hook_enable_prefix_only]])。
7. **idle-in-txn**:profile/corr 读用短事务别跨 await(见 [[reference_flat_idle_in_txn_lock_leak_2026_06_04]])。
8. **survivor bias(review)**:13 样本是「已选出」非「能选全部」→ 注入措辞中性「已覆盖,探索其它」。
9. **OS PnL 失鲜 = single-point-of-failure(v2-rereview MUST②,升为硬 gate)**:`correlation_service` 缓存 TTL 24h;若 ≥7d 失鲜,所有 self_corr = 旧组合噪声 → 整个 A/B 失效。**硬 pre-flight gate(非 caveat)**:启 flag 前查缓存戳(`correlation_service.py:206` `_save_cache('saved_at')`)+ 确认 ≥50 submitted OS alpha 已缓存;失鲜则先手动跑 `refresh_os_correlation_cache` beat 等完成,否则**不启 A/B**。
10. **A+B 同期建归因**:A/B 时 A(prompt)与 B(novelty 记录)分开 telemetry,避免谁起效混淆。
11. **gaming(v2-rereview MUST①,已入 §4 护栏)**:orthogonality_score 可被正交垃圾 alpha 刷高 → §4 GO 加 sharpe-pass 率不降 + interim N40 正交-且-过门黄旗。
12. **infer_pillar 归类 artifact(v2-rereview SHOULD)**:momentum 46% 可能是「`returns` 字段普遍 + PILLAR_VALUES tuple 里 momentum 排第一的 tie-break」人为高估,非真过度集中。**ship 前必做**:人工审 13 个已提交 expression 的 pillar 赋值(尤其 6 个 momentum 是否真重价量、`ts_std_dev(returns)` 类是否被误投 momentum)。审计结果写进本 plan 或单独 doc。NUDGE 用中性「探索其它」措辞兜底 survivor-bias。

---

## 6. 决策(用户已拍板 + review 修正)
1. **范围 = Phase A + B**(C gated-on A/B GO)。
2. **接受 A/B sim 成本**(~60 alpha;挖掘 IDLE 槽空闲)。
3. **现在做**(并行 N1 抽干积压;不等 L1)。
4. **粒度 = 既有 `pillar`(review 推翻 v1 mechanism_family)**:13 样本下 pillar(5 类)比 mechanism_family(6-8 类)密度好、**零新分类器、零 Alembic、Phase A 保持 LOW**。**mechanism_family 推迟到 Phase C**(真要分层 RAG 时)。

---

## 7. Sequencing + 工时

| 档 | 内容 | flag | 风险 | 工时 | gate |
|---|---|---|---|---|---|
| **A**(本期)| pillar 池 profile 聚合 + prompt 负向知识注入 + telemetry + OFF 字节回归测 | `ENABLE_ORTHOGONAL_PROMPT_STEERING` | **LOW** | ~2-3d | shadow ≥5d → A/B ≥2w(interim N40/N80)|
| **B**(本期)| diversity 第 6 维 novelty vs 已提交池(阈值 0.4,初期仅记录)| `ENABLE_SUBMITTED_POOL_DIVERSITY` | MED | ~2d | 与 A 同 A/B 评 |
| **C**(条件)| 边际 ΔSharpe steering + mechanism_family + 分层 RAG | `ENABLE_MARGINAL_STEERING_V2` | HIGH | ~5d+ | **gated-on A/B GO** + 独立 A/B |

**全部 default OFF + 字节不变 when OFF。**

---

## 8. 下一步
**Build Phase A shadow**(pillar 版):flag + `submitted_pool_profile` helper + PromptContext 字段 + prompt 块 + telemetry + OFF 字节回归测。开工前核 OS PnL 刷新时间。
