# 竞品分析:R14 stop-loss 的流水线适配 —— "何时停止一条无产出的挖掘线"(2026-05-27)

承自 `docs/competitive_analysis_v3_2026-05-26.md`(selection vs discovery)。本文聚焦一个更窄的工程决策:**挖掘流水线(producer-consumer)里,R14 task stop-loss 怎么适配连续流**。用外部实践(optimal stopping / 搜索预算分配 / 多重检验 / 工业 kill criteria)给设计分叉定锚,**不改代码,供据此定 build**。

## 0. 问题:R14 在连续流里"round"模糊
现有 R14(`task_stop_loss_service.py` + `config.TASK_STOP_LOSS_*`)是 **round-based**:
- **主 trigger** = `CONSECUTIVE_FAIL_ROUNDS=3`(连续 3 个 zero-PASS round → pause);
- **慢退化兜底** = EMA PASS-rate floor `0.005`(0.5%,production p50=0 标定);
- warmup `MIN_ROUNDS=5`;state 存 `task.config["stop_loss_state"]`(ema / consecutive_zero / rounds_completed)。

legacy FLAT 循环每 round 末调 `check_should_pause(round_pass_count, round_alpha_count)`。**流水线把生成与 sim 解耦** → 一个生成 round 的 PASS 结果是 persister 异步落的,生成下一 round 时还没回来 → "连续 zero-PASS round" 没有现成的同步信号。这是适配的核心难点,也是当前 pipeline 把 R14 列为"延迟项"的原因。

设计分叉(待定):
- **A. 移植连续-zero-PASS-batch**:producer 有天然"批次"(每次生成 round);用 persister 喂回的共享 PASS 计数判定每批是否 zero-PASS,保留 consecutive-zero 语义。
- **B. 累计 pass-rate 刹车**:只用累计 sim/pass 计数,sims≥min 且 pass-rate<floor 则停。
- **C. 跳过 R14**:靠硬上限(`max_iters=100` + `target_candidates=20`)bound 成本。

## 1. Optimal stopping / 序贯检验(SPRT)——"何时该停"的理论底座
**Wald SPRT**(Wald-Wolfowitz 证明最优):序贯累计 log-likelihood ratio,**效应强则早停、效应明显缺失也早停**,达到给定 I/II 类错误下**最小期望样本量**。Ada-SPRT 把它扩到"既选哪个又何时停、带预算约束"。

→ **对 R14 的含义**:R14 的 consecutive-zero-PASS 本质是一个**粗糙的 SPRT**——累计"无效应"证据,够了就停。SPRT 的教训是:**停止判据应基于累计证据的后验,而非固定窗口**——所以 consecutive-zero(累计连续证据)+ EMA(累计率)的组合**方向正确**,比"跑满固定 max_iters"省期望预算。**这否决了纯 Option C**(只靠硬上限=固定样本检验,丢了"明显无效应时早停"的省钱)。

## 2. Successive Halving / Hyperband / ASHA —— 预算应"重分配"非"硬停"
**Successive Halving / Hyperband**(JMLR 16-558,bandit-based):给一批配置均分预算 → 砍掉表现最差的一半 → 把预算**重分配**给幸存者,迭代。**ASHA**(arxiv 1810.05934):**异步**早停 + 重分配,专为大规模并行,用已评估配置预测候选是否有望、**激进早停不阻塞并行**。

→ **对 R14 的含义**(关键洞察):**AIAC 的 dataset-steering bandit(`ENABLE_DATASET_VALUE_BANDIT` + `mining_weight`)已经在做 ASHA 式的"重分配"**——把生成频率从挖尽的 pv1 移向高边际数据源(per-dataset discounted Beta 后验)。所以在**多数据集 FLAT session 里,bandit 才是主效率杠杆(reallocate),R14 是其上的"放弃整个 task"兜底**(当所有数据集都无产出时)。**这说明 R14 不是主成本杠杆,定位是 backstop** —— 不必为它做重型适配;但 backstop 仍有价值(bandit 只在数据集间挪,不会自己停 task)。ASHA 的"异步早停"也提示:R14 的适配应是**异步、不阻塞 producer/consumer 并行**的(producer 侧轻量检查,而非同步等 sim 结果)。

## 3. Deflated Sharpe / 多重检验(López de Prado-Bailey)—— 停止 = 控制试验预算 N
**DSR**(SSRN 2460551):从一组试验里挑赢家会膨胀 max-Sharpe;**N = 有效独立试验数**(高相关试验不算 N 个);**不控制 #trials 就没有清晰停止点**,且假阳性随试验数上升。

→ **对 R14 的含义**:真正的停止纪律是**控制试验预算 N**——这正是 `target_candidates=20`(produced-候选 cap)+ `max_iters` 在做的(见 Option A 的 2c-step2 修复)。**所以"控制 N"这条已被硬上限覆盖**;R14 的增量价值不在"限总数"(已有),而在**"这条 task 的发现率是否高于噪声"**(EMA floor 0.005 ≈ 噪声地板)。DSR 也提醒:PASS-rate 本身要警惕多重检验膨胀,但 R14 用的是粗 PASS 计数 + 保守 floor,作为 kill 信号够用。

## 4. 工业 kill criteria / signal decay —— hit-rate 是核心监控量
顶级 quant(arxiv 2105.01380 "Why and how systematic strategies decay" + 2026 due-diligence 综述):**持续监控 hit-rate / drawdown / 衰减速度**;**研究速度必须跑赢衰减**;有 kill criteria + capacity 上限。allocator 尽调直接看"信号如何发现/验证/多快衰减"。

→ **对 R14 的含义**:R14 的 EMA PASS-rate **就是 hit-rate kill criterion** 的本地实现——工业派印证"低 hit-rate 持续 → kill"是标准做法。所以**保留 R14 的 hit-rate 语义是对的**;它是把工业 kill criterion 落到单 task 粒度。

## 5. 综合 → R14 流水线适配推荐

三方收敛出一致结论:
1. **SPRT**:基于累计证据早停 > 固定样本(否决纯 Option C)。
2. **ASHA/Hyperband**:重分配 > 硬停,且**AIAC dataset bandit 已是 reallocator**,R14 只是"放弃整 task"的异步兜底(不必重型、不该阻塞并行)。
3. **DSR**:控制试验预算 N 已由 target_candidates/max_iters 覆盖;R14 增量 = hit-rate 噪声地板。
4. **工业**:hit-rate kill criterion 是标准,保留其语义。

**推荐:Option A 的轻量异步版 —— producer 侧"连续 zero-PASS 生成批次"刹车 + EMA floor,用 persister 喂回的共享 PASS 计数,异步不阻塞。**
- producer 有天然批次(每次生成 round);persister 落 PASS 时 `incr` 一个共享计数 `{sims, pass}`(已为 daily_goal 协调留了类似 hook)。
- producer 每生成批次后读共享计数,判定上一批是否 zero-PASS(该批 produced 的候选里 persisted-PASS=0),累计 consecutive_zero;达 `CONSECUTIVE_FAIL_ROUNDS` 或 EMA<floor(过 warmup)→ 停 producer(resumable,复用 auth-circuit 那条 `_stop_reason`/return-None 路径)。
- **复用现有 `task_stop_loss_service.check_should_pause/apply_stop_loss_decision`**(传 batch 的 round_pass_count/round_alpha_count),不重写 R14 逻辑——只把"round"映射成"生成批次 + 异步回填的 PASS"。state 仍存 task.config["stop_loss_state"]。
- **异步性**(ASHA 教训):判定用"上一批"的 PASS(那时多半已落库),不同步等当前批 sim;轻微滞后可接受(backstop 性质)。

**否决的备选 + 理由**:
- **Option B(纯累计 pass-rate)**:丢掉 consecutive-zero——而 R14 注释明示 EMA floor 太噪、**consecutive-zero 才是标定过的主 trigger**(production p50 PASS=0,纯 rate-floor 会几乎永不触发或乱触发)。SPRT 也偏好"连续证据"累计。**劣**。
- **Option C(只靠硬上限)**:丢掉早停——SPRT/工业都说"明显无产出要早停省预算",跑满 100 iter 才停浪费 LLM+sim。**劣**(但作为"R14 flag OFF 时"的退化是安全的——硬上限保证不会无限跑)。

**定位提醒**:R14 是 backstop,**主效率杠杆是 dataset bandit(ASHA 式 reallocation)**——若要更大收益,优先确保 bandit 在 pipeline 路径生效(`next_round_inputs` 已接 `weighted_choice`)。R14 适配价值 = 单数据集 task 或全数据集集体无产出时的"放弃整 task"。

## 5b. 扩展方案菜单(应需求,2026-05-27)

设计轴:**信号**(连续-zero / 累计率 / SPRT 似然比 / 成本-per-PASS)× **动作**(停整 task / 重分配 / 踢数据集 / 只报警)× **位置**(producer 批次 / persister / 独立监控)× **复用度**。

| 方案 | 信号 | 动作 | 锚点 | 工作量 | 风险 | 何时最优 |
|---|---|---|---|---|---|---|
| **A 连续-zero-batch 刹车**(推荐基线) | 连续 zero-PASS 生成批次 + EMA floor | 停整 task(resumable) | SPRT 粗版 + 工业 hit-rate | ~半天 | 低(复用 service) | 想忠实移植现有 R14、最小改动 |
| **D 正式 SPRT** | PASS Bernoulli 流的 log-likelihood ratio,H1=产出率≥阈 vs H0=≤噪声;LLR 越下界→停 | 停整 task | SPRT 最优(Wald) | ~1 天 | 中(α/β + 两个率要标定;误标→假停/永不停) | 想要理论最优期望样本、且愿标定 |
| **E 纯靠 bandit(涌现式停)** | 无 task-级信号——无产出 task 经 dataset bandit 权重衰减自然枯竭 → 硬上限终止 | 重分配(已有)+ 硬上限兜底 | ASHA"重分配>硬停" | ~0(只需 bandit ON) | 中(单数据集 task / 全数据集皆烂无兜底) | 多数据集 task + bandit 已调好 |
| **F 成本-per-PASS 经济停** | 累计(LLM+sim 成本)/ PASS 数 > 预算阈 | 停整 task | 工业 capacity / 研究速度>衰减 | ~1 天 | 中(需 per-session 成本计量新面) | 想要经济意义的刹车(成本驱动而非率驱动) |
| **H 两层 ASHA + backstop** | 每数据集激进早停(从 cursor 轮转**踢出**无产出数据集)+ task 级 SPRT/连续-zero | 重分配 + 踢数据集 + 停整 task | 完整 Successive-Halving | ~2-3 天 | 中高(改 cursor 轮转 + 两级状态) | 想要最彻底的预算效率、数据集多 |
| **I 只观测不自动停** | cost-per-PASS / pass-rate telemetry | 只报警,人工 pause | 工业"先监控量化" | ~半天 | 最低(无假停) | 怕自动误停、想先看数据再自动化 |

**组合视角**(非互斥):
- **E + A**:让 bandit 做主重分配(E),A 做"全数据集皆烂"的薄兜底——**我的二选**(贴合"R14 是 backstop"的结论:主力交给已有 bandit,A 阈值可调高,只在集体无产出时停)。
- **I 先行**:先上观测(I,半天),跑 shadow 看真实 cost-per-PASS / pass-rate 分布,再据真实分布标定 A/D/F 的阈值——**避免拍脑袋标定**(呼应 v3 "先建测量再投特性"的教训)。
- **D / F / H** 是"更重但更优"路线,适合 shadow 验证 pipeline 有真实收益后再投。

**轴上的关键取舍**:
- **停 task vs 重分配**:ASHA 明确"重分配优先";AIAC 的 bandit 已是重分配器 → 纯"停 task"的 R14 是次优,**带重分配的方案(E/H)理论上更省**,但 bandit 已覆盖大部分,增量看 shadow。
- **率信号 vs 成本信号**:pass-rate(A/D)简单但忽略"贵的 PASS";cost-per-PASS(F)经济意义强但需成本计量。production p50 PASS=0 下,**率信号几乎全是 0/非 0**,SPRT(D)的似然比会退化得接近连续-zero(A)——所以 D 相对 A 的增量在 p50=0 体制下可能很小。
- **自动 vs 观测**:I 零假停风险,但要人盯;A/D/F/H 自动但有误停风险(需 warmup + 标定)。

## 工作量 / 风险预估(供定 build)
- 复用 `task_stop_loss_service`(零重写)+ 共享 PASS 计数(类比 daily_goal 的 progress dict)+ producer 批次末检查 + 复用 `_stop_reason` 停止路径。**~半天**,中低风险(逻辑复用、flag OFF 不影响 legacy、可单测共享计数+批次判定)。
- 主要风险:异步 PASS 回填的滞后导致 consecutive_zero 判定偏移一批——backstop 性质可接受,单测覆盖"PASS 滞后一批"场景即可。

## Sources
- Hyperband(JMLR 18, 16-558):https://www.jmlr.org/papers/volume18/16-558/16-558.pdf · ASHA "A System for Massively Parallel Hyperparameter Tuning":https://arxiv.org/pdf/1810.05934
- SPRT(Wikipedia):https://en.wikipedia.org/wiki/Sequential_probability_ratio_test · Ada-SPRT(arxiv 1708.08374):https://arxiv.org/abs/1708.08374
- Deflated Sharpe Ratio(Bailey-López de Prado,SSRN 2460551):https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551 · https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf
- "Why and how systematic strategies decay"(arxiv 2105.01380):https://arxiv.org/pdf/2105.01380 · Quant due-diligence 2026:https://resonanzcapital.com/insights/quant-hedge-funds-in-2026-a-due-diligence-framework-by-strategy-type
- 承接:`docs/competitive_analysis_v2_2026-05-19.md`、`docs/competitive_analysis_v3_2026-05-26.md`
