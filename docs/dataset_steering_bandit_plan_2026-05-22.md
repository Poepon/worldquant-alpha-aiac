# 数据集导流 bandit 实施 plan v3（广度方向）

> **日期**：2026-05-22 · **v3**：经 3 轮 fresh-agent review（可行性 + 设计 + 收敛）— v1 7 项 + v2 收敛 6 项全修，verdict fix-then-implement→buildable
> **承前**：[`competitive_analysis_v2_2026-05-19.md`](competitive_analysis_v2_2026-05-19.md) §7.1.4 FactorMiner · fresh-agent 竞品分析（iSharpe/Fundamental-Law + FactorMiner/AlphaForge/Feng-Giglio-Xiu/DPP）
> **依赖**：dataset-attribution（A+B，2026-05-22 已 ship + 回填 5776 行）

## Context

挖掘机械生成层已修尽/证伪，真瓶颈 = **字段级 edge + 可提交边际价值**。实证：真可提交（can_submit + IQC Δsharpe>0）仅 ~15；dataset 选择**实质随机**（`mining_weight` 恒 1.0）；**pv1 被挖烂**（2280 sims→1 个 IQC-边际正 = 0.04%，vs fundamental6/analyst4 0.8%，新 alpha 与 book 冗余）。

**目标**：dataset 选择 随机→自适应导流（off pv1，向高边际价值+正交源），保多样性（提交硬约束 self-corr<0.7 + prod-corr）。**最优解**（两 fresh-agent 收敛）：把 dormant `mining_weight` 变 discounted-Thompson bandit reward + 多样性集合目标。

---

## ⚠️ 关键机制纠正（v2，review C2/C3）

`mining_weight` 的 `ORDER BY ... DESC, random() LIMIT 10`（mining_tasks.py:692）**只决定哪 10 个进候选池**；之后 FLAT **round-robin 等概率挖全部**（`datasets[flat_cursor % len]`，:1273），ONESHOT 也等概率遍历。**所以单改 mining_weight ≠ 挖掘频率导流**（≤10 数据集时零效果；只能把 pv1 二元挤出 top-10）。

→ **Tier A 必须改 FLAT/ONESHOT 循环为按权重加权采样**（不是"选择路径不动"）。另：`ENABLE_DAG_TRACE` ON 时 per-iteration anchor 由 DAG-UCB 覆盖（mining_tasks.py:1274，dormant，需 acknowledge）。

---

## Tier A — discounted Thompson 写回 mining_weight + 加权采样（~2.5 人日）

### A1. schema（Alembic，off head `q8f0d4c2e9b3`）
`bandit_state`（PK region+dataset_id）加：`alpha_param` Float default 1.0、`beta_param` Float default 1.0、`pulls_at_last_refresh` Int（pull-indexed 衰减用）。保留 pulls/total_reward 兼容。

### A2. reward = book-边际 Bernoulli 成功率（review C1/C2/S4 修正）
**单一嵌套成功定义**（去掉 w1/w2/w3 加权和的双计/维度错）：
```
S_d = #(IQC-marginal Δsharpe>0 可提交 alpha)   在 refresh 窗口内   # 真目标(稀疏)
T_d = #(真 BRAIN sims)  ——必须排除 PRESIM_SKIP / metrics._pre_brain_skip   # review N3
```
`S_d ∈ [0,T_d]` 由构造保证 → β 永正。**v1 reward = 纯 `S_d/T_d`，无连续 bump**（review SF-1：κ·残差 是连续值进计数 α，重新引入维度问题且破坏后验可解释性——v1 不要）。

**v1 reward 语义（review C-2，关键）**：S_d 极稀（~15/全系统、pv1 1/2280），所以 v1 **不是"学哪个新源好"，而是"去优先证明冗余的 pv1 + 探索其余"**：
- pv1 的 2280 sims 里 62 个 can_submit-但-IQC≤0（冗余）→ 全进 `T_d−S_d`=**dense β failure** → θ_pv1 快速↓（这是去优先 pv1 的强信号，不稀疏）。
- 新/欠挖源 T_d 小 → 留在 seed 先验附近 → 被 Thompson 探索。
- **⚠️ 不要把 reward 换成 can_submit 计数**（reviewer 建议 (a)，但**错**）：can_submit 含 pv1 那 62 个冗余 → 会**奖励** pv1，与目标相反。S_d 必须是 IQC-marginal-正。
- **主动"找好源"= phase-2**：残差 Sharpe 作**整数伪计数** `S_d += round(κ·max(0,dense_d))`（dense_d=mean(metrics['_r13_residual_sharpe'])，从 JSONB 取，非死表），gate 在 `ENABLE_FACTOR_LENS` + per-region parquet 就绪。

### A3. discounted Thompson（pull-indexed，review S1 修正）
```
# pull-indexed 衰减(只在该臂被拉时衰减,未挖源不被日历漂走;自动:pv1 快遗忘/长尾不漂)
g = γ^(pulls_d − pulls_at_last_refresh_d),  γ≈0.95
α_d ← g·α_d + S_d;   β_d ← g·β_d + (T_d − S_d)              # v1 无 bump(SF-1)
pulls_at_last_refresh_d ← pulls_d                            # 记账, 下窗只衰减新增 pull
θ_d = Beta(α_d, β_d) 采样
mining_weight(d) = θ_d + floor_decay(d);  floor_decay = c·exp(−sims_redundant_d/τ)
```
- **先验 seed（review N2）+ 防抹（review C-1）**：α/β 从回填的 5776 行历史 value-yield 初始化；**同时 seed `pulls_at_last_refresh_d = 该源累计历史 sims`**（与 seed 同分母）→ 首次 refresh `g=γ^(新增 sims)=γ^0=1`，**不抹 seed**。否则 pv1 首窗 γ^2280≈0 秒清先验（seed 与 discount 相互抵消的坑）。
- Thompson 采样**自带探索**，无需 UCB；pull-indexed 衰减处理非平稳（内生自挖烂）+ 吞掉旧 FLAML cost-factor。

### A4. 加权采样（C2 核心修正）
- **写回 `DatasetMetadata.mining_weight`**（让 ORDER BY 选出高价值 top-10 成员）。
- **改 `_run_flat_iteration` 的 round-robin → 按 mining_weight 加权采样** dataset（ONESHOT 循环同理）→ 真频率导流。flag-gated（OFF 回退等概率 round-robin = byte-for-byte 兼容）。

### A5. beat job `refresh_dataset_mining_weight`
- 新 `backend/tasks/`：算 S_d/T_d → A3 更新 → 写回 mining_weight。**DB-only、与 sync 顺序无关**（已验证 sync_datasets 不动 mining_weight；solo 串行队列下精确分钟不 load-bearing，review S3）。`crontab` 日频。
- **flag `ENABLE_DATASET_VALUE_BANDIT`**（config.py + feature_flag_service SUPPORTED_FLAGS 双注册）：OFF→不写+等概率采样=兼容；ON→导流。

### A6. 关键文件
- `backend/selection_strategy.py`（`DatasetBandit`：Thompson reward；`arms` 是 `Dict[str,DatasetArm]`，且 `DatasetArm` 字段是 `total_pulls` 非 `pulls`，review N-1）
- `backend/dataset_selector.py`（reward 编排；**重写坏的 `_save/_load_bandit_state`@:499/:527**——现有 `for arm in self.bandit.arms` 迭代 dict **key**→`arm.dataset_id` AttributeError 被静默吞，从未真持久化，review S2/N-1）· `backend/tasks/dataset_weight_refresh.py`（新 job）
- `backend/tasks/mining_tasks.py`（`_run_flat_iteration` 加权采样 + ONESHOT 循环）
- `backend/models/knowledge.py`（bandit_state +列）+ Alembic · `config.py`+`feature_flag_service.py`（flag）

---

## Tier B — 相关惩罚贪心 / DPP（多样性正确，~3 人日，Tier A 验证后）

标量权重结构上无法表达多样性（集合属性）。**N=17 直接用相关惩罚贪心（MMR），DPP 降可选**（review S2）：
```
每轮贪心选 K 个 dataset:  每步选 argmax_d [ θ_d − ρ·max_{s∈已选} C_ds ]
  θ = Tier A Beta 采样(质量);  C = 源-源相关核(每对 dataset 已挖 alpha 平均 pairwise self-corr)
  MMR 选择步 O(K·N²) 便宜；但**构建 C 不便宜**(review SF-2)
```
- **C 核构建成本**：per-alpha PnL 是 BRAIN 实时拉(`correlation_service`，非存储)，现仅 ~15 OS alpha 有缓存——不足以估 17×17 源对相关。Tier B 须**先预计算/持久化** mined-alpha 按 dataset 分组的 corr（新表或扩 OS 缓存），并预算 PnL 拉取成本。**别把 C 当"极便宜"**。
- **DPP 可选**（`log det(diag√θ·C·diag√θ)`）：须先把 C **投影到最近相关阵 / 向 I 收缩**保 PSD；且 log-det **非单调非非负 → (1−1/e) 保证不成立**（删除 v1 的该错误论断）。MMR 在 N=17 不输 DPP 且更稳。
- **必须同时替换 `_get_datasets_to_mine` AND `_get_complementary_datasets`**（:700 也 ORDER BY mining_weight，否则补充池重新引入模块化 top-K，review N1）。Tier B ON 后 mining_weight 沦为**展示/观测用**（集合感知选择无法用 ORDER BY 复现）。
- **粒度警告**（review S3）：dataset 级是**有损代理**——冗余本质在字段/表达式级（不同 dataset 的 alpha 可近重复；同 pv1 内字段可正交）。**真多样性保证仍是提交端 self-corr/prod-corr 门**；Tier B 只**减少**（非消除）冗余源上的浪费 sims。字段级（复用 `FieldSelector`/`DiversityFilter` 的 field-kernel）才是 SOTA 终态，dataset 级为廉价过渡。

---

## 不做
contextual/RL（17 臂稀疏滞后，信号不够）；硬 forbidden-region 永久禁（submit 门已强制正交，软禁+斥力够）。

## 验证
1. **dry-run** beat job：打印新 mining_weight + Beta 分布，确认 pv1 << fundamental6/analyst4 且无源饿死（floor）+ β 全正（防 C1 回归）。
2. **影子验** dense/残差项是否追踪真 IQC-marginal（信任 κ>0 前）。
3. flag ON 观察 ≥1 周：**v1 真实可达成功指标 = pv1 sims 占比↓ + 多样性不退（self-corr 分布）+ 欠挖正交源 sims↑**。**"IQC-可提交产出↑" 是 phase-2（残差 reward）目标，v1 不承诺**（review C-2：v1 信号太稀只能去优先 pv1，不能主动学好源）。
4. 单测：Thompson/pull-indexed 衰减数学（**断言 β>0**）+ seed 后首窗 g=1（防 C-1 抹 seed）、reward（real-ORM bandit_state read-back，含重写的 _save/_load）、PRESIM_SKIP 排除、加权采样 flag **OFF byte-for-byte**（ON 路径是随机采样**不可位置级断言**，改 seed RNG 或卡方分布测，review SF-3）。回归 0 漂移。

## Rollout
Tier A(~2.5d, v1 先不含残差项) → flag ON 影子观察 → 残差项 phase-2(gate R13) → Tier B(~3d, MMR)。

## 衔接
`selection_strategy.DatasetBandit`（**dormant 死代码**，零 live caller——非"喂 PASS 率"，无冲突，review S1）→ 换 reward+写回+修 _save/_load。FactorMiner(competitive §7.1.4)=最相关 prior art。残差项接 R13 factor-lens（**须先 ENABLE_FACTOR_LENS + 补 per-region parquet**，现仅 usa；review C1）。依赖 dataset-attribution A+B。`FieldSelector`(selection_strategy.py:207)=字段级同构扩展(future)。

## 风险
- 非平稳→pull-indexed γ；reward 稀疏→先验 seed + 残差 bump(phase-2)；多样性→Tier B(MMR)。
- **4330 未解析 dataset_id**（review N-2 纠正 v2）：`derive_dataset_id` 现返回 None → 未解析行**被排除**出所有臂的 T_d（不偏 pv1，是安全的）。**反而不要加 pv1/universal-PV fallback**——加了会把误归的裸 PV sims 灌进 pv1 的 T_d 分母。保持排除即可。
- 残差项依赖 R13 shadow 精度 → flag-ON gate 在 R13 可信（review N5/C1）。
- DAG-UCB（ENABLE_DAG_TRACE）竞争 anchor 选择（dormant）。

## changelog
**v2**（review round 1）：C1 reward→Beta 维度错→Bernoulli S_d/T_d；w3 死表→metrics 取/v1 drop。C2 mining_weight 只控 top-10→加循环加权采样。S1→pull-indexed。S2 _save/_load bug→重写。S4 w1⊂w2→嵌套。Tier-B DPP→MMR 主。N3 trials 排 PRESIM_SKIP。
**v3**（review round 2 收敛）：**C-1** seed 时同 seed `pulls_at_last_refresh=历史 sims`（防首窗 γ^2280 抹 seed）。**C-2** v1 删 κ-bump(SF-1)+纯 S_d/T_d，reframe v1 目标=去优先 pv1+探索(非 submittable↑)，**拒绝 densen 到 can_submit**(会奖励 pv1 冗余)，主动找好源=phase-2 残差。**SF-2** Tier-B C 核非便宜(PnL 实时拉)→须预计算。**N-2** 4330 未解析行排除即安全，**别加** pv1 fallback(v2 写反)。**N-1** _save/_load 在 dataset_selector.py。
