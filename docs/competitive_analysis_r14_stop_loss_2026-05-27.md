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

## 5c. 过拟合风险筛选(应需求,2026-05-27)

**判据**(DSR / 多重检验支柱的直接应用):一个停止规则若**基于噪声主导的 in-sample 信号做自动决策、且其阈值拟合到历史数据**,则该决策会过拟合短期噪声——在 **production p50 PASS=0** 体制下,pass-rate 信号几乎全是噪声,任何"按 pass-rate 停 task"都是在**把短期 dry-spell 误判为无产出**(假停),不泛化。**过拟合-free 的停止 = 预先承诺的预算上限**(prior 决策,与数据无关),而非 metric-反应式 kill。

| 方案 | 过拟合风险 | 判定 |
|---|---|---|
| **A 连续-zero-batch + EMA floor** | **中-高**:consecutive=3 / floor=0.005 是拟合到 production 的阈值;p50=0 下"3 连零"对好 task 的正常 dry-spell 也常发生 → 假停=过拟合短期噪声 | ❌ 踢除 |
| **D 正式 SPRT** | **高**:必须从稀疏历史 PASS 估 H0/H1 两个率 + α/β——稀疏数据估率是过拟合陷阱本身;p50=0 下似然比退化且率估计极不稳 | ❌ 踢除 |
| **F 成本-per-PASS** | **高**:分母是噪声 PASS 数(p50=0 → 除零/爆值 → 几乎必触发);阈值若拟合历史 cost-per-PASS 分布则过拟合 | ❌ 踢除 |
| **H 两层 ASHA + SPRT backstop** | **高**:含 D 的 SPRT + **按噪声 per-dataset 率激进踢数据集**(少数零-PASS 轮就踢=过拟合 per-dataset 噪声,比 task 级更脆) | ❌ 踢除 |
| **E 纯靠 bandit + 硬上限** | **低**:停止来自**预先承诺的硬上限**(max_iters/target_candidates,prior 非数据反应=DSR-clean 的"控制 N");bandit 是**软降权 + 折扣 Beta 后验(带悲观先验、跨整 session 累积、不硬停、可自愈)**,远比 3-batch 硬触发稳 | ✅ 保留 |
| **I 只观测不自动停** | **无**:不做自动决策,人工判断 | ✅ 保留 |

**踢除后的结论**:过拟合-free 的集合 = **E(bandit 软重分配 + 预算硬上限)+ I(观测,人工)**。两者恰好都**不含"拟合到噪声 pass-rate 的自动停 task 规则"**。

**关键洞见**:
1. **真正抗过拟合的停止纪律是"预先承诺的试验预算上限"**(DSR 的"控制 N trials")——而它**已经在 pipeline 里实现了**(`max_iters=100` + `target_candidates=20`,见 2c-step1/2)。所以"过拟合-free R14"其实**主体已就位**(= E 的硬上限部分 + 已接的 dataset bandit)。
2. **增量 = I(加 cost-per-PASS / pass-rate telemetry 供人看)**——它本身零自动决策=零过拟合,且产出的数据让人能**事后**判断,而非用噪声阈值**事前**自动停。
3. 换言之:**不新增任何"按 metric 自动停 task"的规则**(A/D/F/H 全踢);保留已有的预算上限 + bandit 软重分配(E),再加观测(I)。这与"R14 是 backstop、主杠杆是 bandit"的结论完全一致,且**用过拟合判据独立推到了同一落点**。

## 6. 重构:多样性-最大化预算分配(自动 + 低过拟合 + 高多样性,2026-05-27)

需求:**不要人工(踢除 I)、低过拟合(踢除 A/D/F/H 的噪声-pass-rate 自动停)、高多样性**。这三约束**本身就选定了方案族**:自动→算法分配;低过拟合→信号必须**结构性**(覆盖/新颖性/相似度,非噪声 pass-rate);高多样性→多样性即显式目标。= **Quality-Diversity / 新颖性搜索 / DPP** 族。**问题从"何时停"重构为"如何把已封顶的预算(max_iters/target_candidates 仍是抗过拟合的硬上限)自动花在最大化多样性上"。**

| 方案 | 机制 | 为何低过拟合 | 为何高多样性 | 锚点 / AIAC 已有件 | 工作量 |
|---|---|---|---|---|---|
| **Q MAP-Elites / QD steering** | 行为空间=广度轴(数据源×region×delay×pillar);每 cell 存最佳 elite;producer 把生成预算导向**空/可改进 cell**(illuminate 全空间) | 分配信号=**cell 覆盖度(结构计数)**,非 pass-rate;保 elite 不按率停 | 多样性是**字面目标**(全广度空间每 cell 一个 elite) | MAP-Elites(1504.04909)+ v3 广度=数据源 + self-corr 门;dataset bandit cells / diversity_tracker | 中-高 |
| **R DPP 多样 sim-批选择** | sim 槽是稀缺资源;在已生成候选里用 **DPP/贪心 max-min**(表达式指纹相似核)选**最多样**的去 sim,低冗余 | 相似核是**结构指纹**,非 performance | DPP repulsion = 多样性即构造 | DPP batch AL(1906.07975)+ diversity_tracker 指纹 | 中 |
| **S 新颖性-reward 导流 bandit** | 把现有 dataset/cell bandit 的 **reward 从 binary can_submit 换成 新颖性/正交性**(该 cell 近期 alpha vs 已提交池的平均距离 / self-corr) | 新颖性是**更稠密、更少噪声**的结构信号(PASS 稀疏 p50=0,新颖性密) | 奖励探索**不相关区域**=直接 self-corr<0.7 的货币 | 新颖性搜索(Lehman-Stanley)+ v3 self-corr 门;**bandit 已在 next_round_inputs 接好**,只换 reward fn | 中(低,复用 bandit) |
| **T 分层覆盖 round-robin(零 metric)** | 按广度 cell(数据源×region×delay)分层轮转花预算,**完全不用 performance 信号** | **零拟合规则**(无 metric) | 覆盖多样性(均匀铺满广度) | 分层抽样 + DSR 控-N;cursor%len 已是粗 round-robin | 低 |

**组合(非互斥)**:
- **S + R** —— **我的首选**:S 决定**从哪个 cell 生成**(新颖性-reward 导流,复用已接的 bandit,只换 reward),R 决定**生成出的候选里哪些值得占稀缺 sim 槽**(DPP 选最多样)。两者都自动、低过拟合、高多样性,且**叠在已有预算硬上限之上**(硬上限=抗过拟合的"停",S/R=抗过拟合的"花在多样性上")。
- **Q** 是把 S/R 统一到一个 illuminate-全空间 的框架(最完整、最大工作量),适合 shadow 验证 S/R 有真实多样性收益后再升级。
- **T** 是零风险地板(纯覆盖),可作 S 的冷启/兜底(bandit 没数据时先分层铺)。

**与"踢除"结论的一致性**:硬上限(预算)仍是抗过拟合的停止纪律(保留);**新增的不是"按率停"而是"按结构性多样性信号花"** —— 完全绕开了 A/D/F/H 的噪声-pass-rate 过拟合面,且 reward/选择用的是新颖性/相似度(结构信号,p50=0 下远比 PASS 稠密稳健)。

**关键取舍**:
- **S 的 reward 定义**:用 self-corr vs 已提交池(v3 的绑定门货币,最贴价值)还是表达式指纹距离(更便宜但离"真正交"远一层)?self-corr 更准但需 correlation_service 在导流时可算;指纹距离便宜可即用。
- **R 的核**:表达式指纹(便宜、即用)vs PnL 相关(准但需先 sim——鸡生蛋)。pre-sim 只能用指纹核;PnL 核要 sim 后才有 → R 在 pre-sim 阶段用指纹,post-sim 的 self-corr 仍由提交门把关。
- **过拟合残余**:新颖性信号本身也可能被"刷新颖性"游戏(生成无意义但指纹远的 alpha)——需配合既有 semantic validator + 提交门(self-corr 是 vs **真实**提交池,无法刷)兜底。

## 7. 经济意义刹车,去过拟合化(2026-05-27)

需求:**也要经济意义的刹车**,但须满足前面的约束(自动 + 低过拟合 + 高多样性)。原 **F(cost-per-PASS)被踢的根因 = 比率的分母是噪声 PASS 数**(p50=0 → 除零/爆值/拟合历史分布)。修法是把经济信号从「比率」拆成两个**各自去过拟合**的件:

### F′ — 成本预算上限(过拟合-free 的经济"停")
- **机制**:累计真实成本(LLM token × 价 + sim 数 × 日配额占比)≥ 预设预算 `SIM_PIPELINE_COST_BUDGET` → 停 producer(resumable)。
- **为何低过拟合**:**预先承诺的天花板(prior 决策)+ 分子确定**(token/sim 计数确定,非噪声)+ **无 PASS 分母** → 这就是把现有 count-based 硬上限(max_iters/target_candidates)**升级成 $-有意义的预算**,抗过拟合性等同(都是 DSR 的"控制 N/预算"纪律,不反应 metric)。
- **为何不丢多样性**:它只封**总花费**;花在哪由 S/R 的多样性信号决定。
- **AIAC 已有件**:`cost_tracker`(G2 contextvar 计量)+ `MAX_TOKENS_PER_DAY`/`MAX_SIMULATIONS_PER_DAY` → 复用,低工作量。

### margin-as-quality — 经济价值进 reward(而非另起噪声 kill)
- **机制**:把经济价值用 **margin(bps,`marginal_analysis.py` 的 is_margin;地板≈5bps 真实交易成本,<0 无提交价值)** 注入 S 的 reward / Q 的"quality"维度:`reward = 多样性(正交性) × 经济价值(margin 超过 cost-positive 地板)`。
- **为何低过拟合**:**margin 是连续且每个 simmed alpha 都有(比二元稀疏 PASS 稠密得多)**;地板 5bps 是**真实交易成本(经济常数,非拟合历史 performance)** → 阈值不过拟合,估计也因信号稠密而更稳。
- **为何高多样性**:这正是 **Quality-Diversity 的"quality"= 经济 margin、"diversity"= 广度 cell** —— 导流偏向**又多样又 cost-positive** 的区域(MAP-Elites 1504.04909 的 quality 维设成 margin)。
- **AIAC 已有件**:`marginal_analysis.py` 的 margin/is_margin + per-region scales。

### 修订后的合成(自动 + 低过拟合 + 高多样性 + 经济)
```
总花费天花板:  F′  成本预算($,预先承诺,无噪声分母)         ← 经济"停"
花在哪/生成:    S  bandit reward = 正交性 × margin(超 5bps)    ← 多样性×经济价值
占哪个稀缺槽:    R  DPP 指纹核选最多样候选去 sim                  ← 多样性
```
- **三件都自动、低过拟合、高多样性,且经济意义体现在 F′(预算)+ S 的 margin-quality(价值)两处**,均**绕开噪声 pass-rate**。
- vs 原 F:**F′ 是绝对预算(无分母)不是 per-PASS 比率;经济价值改用稠密的 margin 而非稀疏 PASS** → 去过拟合化。

**残余取舍**:
- margin 也可被"高 margin 但同质"的 alpha 占据 → 所以是 **正交性 × margin 的乘积**(既要多样又要经济),非单 margin。
- margin 在 sim 后才有 → 它进的是**下一轮的 cell reward**(per-cell 累积),不是当轮门;当轮多样性靠 R 的指纹核(pre-sim 即用)。
- F′ 的预算值要拍:用真实 token 价 + 日配额折算,**按经济(不按历史 pass 分布)设**,避免把 F′ 的阈值也过拟合。

## 工作量 / 风险预估(供定 build)
- 复用 `task_stop_loss_service`(零重写)+ 共享 PASS 计数(类比 daily_goal 的 progress dict)+ producer 批次末检查 + 复用 `_stop_reason` 停止路径。**~半天**,中低风险(逻辑复用、flag OFF 不影响 legacy、可单测共享计数+批次判定)。
- 主要风险:异步 PASS 回填的滞后导致 consecutive_zero 判定偏移一批——backstop 性质可接受,单测覆盖"PASS 滞后一批"场景即可。

## Sources
- Hyperband(JMLR 18, 16-558):https://www.jmlr.org/papers/volume18/16-558/16-558.pdf · ASHA "A System for Massively Parallel Hyperparameter Tuning":https://arxiv.org/pdf/1810.05934
- SPRT(Wikipedia):https://en.wikipedia.org/wiki/Sequential_probability_ratio_test · Ada-SPRT(arxiv 1708.08374):https://arxiv.org/abs/1708.08374
- Deflated Sharpe Ratio(Bailey-López de Prado,SSRN 2460551):https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551 · https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf
- "Why and how systematic strategies decay"(arxiv 2105.01380):https://arxiv.org/pdf/2105.01380 · Quant due-diligence 2026:https://resonanzcapital.com/insights/quant-hedge-funds-in-2026-a-due-diligence-framework-by-strategy-type
- 承接:`docs/competitive_analysis_v2_2026-05-19.md`、`docs/competitive_analysis_v3_2026-05-26.md`
