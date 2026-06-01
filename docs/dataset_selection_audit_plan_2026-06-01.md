# Dataset 选择产能崩 audit plan

- 日期: 2026-06-01
- 状态: **调研 plan(未开工)** — 写假设清单 + 验证方法,不动代码
- 起源: 2026-06-01 orchestrator endpoint live 返回 USA 7d 0 PASS / weight 0.002,深挖发现 mining 自身真产能崩
- 优先级: 翻 `ENABLE_AUTO_ORCHESTRATOR=True` 之前必须修(orchestrator 会反复 launch USA → 反复产 1.1% 有价值 alpha)

---

## 1. 实证现状(2026-06-01 DB probe)

### 1.1 产能数据
| 窗口 | 总 | PASS | PASS_PROVISIONAL | 有价值率 |
|---|---|---|---|---|
| 7d USA mining-direct | 445 | **0** | 5 | **1.1%** |
| 7-30d USA mining-direct(基线) | 1435 | 118 | 568 | **47.8%** |
| 14d 每日 | ≤ 180 | 0 | 0-5 | **≤ 3.4%/天** |

**产能 ~45× 下降**,且**不是 7d 突发**——14d 内每天都低,说明~2 周前(`2026-05-19` 之前)就崩了。

### 1.2 Dataset 集中
| Dataset | 7d alpha | quality |
|---|---|---|
| **pv1** | **196 (44%)** | 195 FAIL + 1 PROV |
| fundamental2 | 39 | 全 FAIL |
| news18 | 33 | 全 FAIL |
| fundamental6 | 33 | 31 FAIL + 2 PROV |
| socialmedia12 | 23 | 全 FAIL |
| option8 | 22 | 全 FAIL |
| analyst4 | 20 | 全 FAIL |
| model53 | 20 | 全 FAIL |
| news12 | 17 | 全 FAIL |
| 其余 12+ dataset | < 10 each | 全 FAIL |

pv1 占近一半,其余 dataset 散落,**且没有任何 dataset 出 PASS**。

### 1.3 Bandit weight vs 实际选择脱钩
USA delay=1 TOP3000 `dataset_cell_stats.mining_weight` top 10(`ENABLE_DATASET_VALUE_BANDIT` 状态待 grep 确认):
```
univ1       0.1406
model51     0.0996
socialmedia8 0.0547
option9     0.0466
model77     0.0397
model53     0.0328
option8     0.0306
news12      0.0245
sentiment1  0.0195
pv13        0.0176
```
**pv1 不在 top 10**,但实际产出 44%。bandit 信号没作用到 producer。

### 1.4 FAIL 性质(4 个 sample)
全是 LOW_SHARPE + LOW_FITNESS 双 FAIL:
- sh=-0.11, ft=-0.03(pv1)
- sh=-0.49, ft=-0.25(pv1)
- sh=-0.46, ft=-0.17(pv1)
- sh=-0.69, ft=-0.30(pv1)

**真烂 alpha**,不是 verdict 误判 / 阈值过严问题。

### 1.5 任务来源
7d 启 8 个 USA task 全 `target_datasets=AUTO`(start_flat_session 不传 datasets → `_get_datasets_to_mine` 自动选)。混合:
- 流水线(`enable_sim_pipeline=true`):4 task,46 alpha
- 串行(legacy `_run_flat_iteration`,`enable_sim_pipeline=None`):4 task,150 alpha

**两条路径同样集中 pv1** — 不是流水线特定 bug。

---

## 2. 关键代码位置(2026-06-01 实测)

### `_get_datasets_to_mine` (mining_tasks.py:701-747)
```sql
SELECT dataset_id FROM datasets
LEFT JOIN dataset_cell_stats ON ... universe=task.universe AND delay=...
WHERE region=task.region
ORDER BY COALESCE(mining_weight, 1.0) DESC, random()
LIMIT 10
```
返回 **top-10 dataset 列表**(不是单个)。

观察:
- `COALESCE(mining_weight, 1.0)` 默认 1.0 给无 cell dataset
- top 10 weight ~ 0.14 / 0.10 / 0.05 / ... < 1.0 → **pv1 cell 无 weight(没 sync 过?)→ COALESCE=1.0 → 排第一**

### Producer dataset 选择(`_run_flat_iteration_pipeline` 内 next_round_inputs)
- bandit_on=`ENABLE_DATASET_VALUE_BANDIT`
- bandit ON → `weighted_choice(datasets, ds_weight_map)` override
- bandit OFF → `_pick_dataset(datasets, _coverage, _category_coverage, _category_of, _reward, _explore_prob, rng)` ε-greedy

`_pick_dataset` (mining_tasks.py:1275-1288) 选择 [datasets] 参数即 `_get_datasets_to_mine` 返回的 list。如果 list 头部是 pv1,coverage 平铺前 pv1 会被先打开。

### datasets table 的 mining_weight
`models/metadata.py:70` `mining_weight = Column(Float, default=1.0)` — 但 cell-stats 规范化(2026-05-26 cutover)后,weight 移到 `dataset_cell_stats` per-(universe, delay)。可能 cell 同步漏了一批 dataset(包括 pv1)。

---

## 3. 假设清单(H1-H6 + 验证方法)

### H1 — pv1 cell 没 sync,COALESCE=1.0 让它登顶 ⭐ 高概率

**假设**:`dataset_cell_stats` 没有 pv1 USA TOP3000 delay-1 cell 行(`cell_stats_normalization` cutover 漏了 pv1),`COALESCE(mining_weight, 1.0)` 让 pv1 weight 显示为 1.0,比 bandit top weight (univ1=0.14) 高 ~7×,所以 ORDER BY DESC 把 pv1 排第一。

**验证**(单 SQL):
```sql
SELECT cs.* FROM dataset_cell_stats cs
JOIN datasets d ON d.id = cs.dataset_ref
WHERE d.dataset_id='pv1' AND d.region='USA'
  AND cs.universe='TOP3000' AND cs.delay=1;
```
0 行 → H1 确认。

**修复**:补 pv1 cell + 更新 mining_weight(让 bandit 反馈到 pv1)。

### H2 — bandit 反馈环挂了,pv1 cell 存在但 weight 没被降

**假设**:pv1 cell 存在,且 bandit refresh task(`run_dataset_weight_refresh` daily 05:15)有跑,但反馈算法 bug 让 pv1 的 weight 没降(or 比预期高)。

**验证**:
1. `cs WHERE dataset_id='pv1' AND universe='TOP3000' AND delay=1` 看 mining_weight + last update time
2. 看 `_pre_brain_skip` count(memory:reward 排除 pre-sim skip),pv1 真挖 195 个,reward 应该全 0/195 = 0
3. `ENABLE_DATASET_VALUE_BANDIT` 当前是否 ON

**修复**:audit `run_dataset_weight_refresh` 代码 + bandit 反馈逻辑。

### H3 — `_pick_dataset` ε-greedy 在 coverage 平铺时仍偏 pv1

**假设**:即使 `_get_datasets_to_mine` 返 10 个 dataset 列表,producer 内 ε-greedy 因 `_coverage` 字段 bug 没正确平铺(比如每个 task 内 coverage 重置,导致每 task 都从 dataset[0]=pv1 开始)。

**验证**:看 trace `mining_tasks.py:_run_flat_iteration` 内 `_coverage` 是 session-local。memory [[reference_rag_retrieval_dormant_layers]] 风格:加 log 看 producer 每次 next_round_inputs 真选的 dataset 序列。

也可以 SQL:看 pv1 196 个 alpha 是 8 task 平均分配(每 task ~24)还是单 task 暴产(单 task 100+):
```sql
SELECT task_id, COUNT(*) FROM alphas
WHERE region='USA' AND task_id IS NOT NULL
  AND created_at >= NOW() - INTERVAL '7 days' AND dataset_id='pv1'
GROUP BY task_id ORDER BY 2 DESC;
```

**修复**:看具体 _pick_dataset 内部状态机。

### H4 — `_get_complementary_datasets` 把 pv1 加进所有 task 的 pool

**假设**:anchor dataset 选了别的,但 `_get_complementary_datasets` 总把 pv1 当 complementary 加进 pool(因为 pv1 mining_weight=1.0 高)→ 每 task 池都有 pv1 → producer 总能选到。

**验证**:看代码 (mining_tasks.py:750-794),看 SQL 是否 pv1 总在 complementary top-K。

**修复**:加 dataset spread cap。

### H5 — start_flat_session 没接 orchestrator,user 手动只开 USA

**假设**:7d 8 个 task 都是 USA 因为用户手动只开 USA(memory:用户单 task 长跑模式)。这不是 bug 而是 user 行为。但 dataset 集中仍是 mining 自身的 bug。

**验证**:看 `mining_tasks.created_by` 字段(如有)或 task_name 模式。已知 7d 全 `flat-session-USA-*`。

**结论**:确认是 user manual,但**不影响 dataset 集中 bug** 这一更深层问题。

### H6 — pv1 数据真的 14d 都挖不出 PASS(战略级)

**假设**:即使 dataset 选择算法完美 spread,pv1 / 其他价量数据集本身 edge 挖尽(memory [[project_depth_levers_refuted_breadth_is_answer]] 已论证)。30d 基线 47.8% 是更早数据,~3 周前 BRAIN edge 还在;现在普遍下降是市场 alpha-decay。

**验证**:30d-90d 三窗口分桶:
```sql
SELECT 'recent'  AS w, count, pass FROM ... WHERE created_at > NOW() - 14d
UNION ALL
SELECT 'mid',    ... WHERE created_at > NOW() - 30d AND < NOW() - 14d
UNION ALL
SELECT 'old',    ... WHERE created_at > NOW() - 90d AND < NOW() - 30d
```

**修复**:战略级 — 加新正交数据源,memory [[reference_competitive_analysis_v3]] 已推荐"广度=数据源"。

---

## 4. 修复路径(按假设结果分支)

| 主要假设 | 修复 | 工作量 |
|---|---|---|
| **H1**(cell 缺) | 补 pv1 cell + sync,改 COALESCE 默认从 1.0 → 0(无 cell 不参与)或 prior 0.5 | 0.3-0.5d |
| **H2**(bandit 挂) | audit `run_dataset_weight_refresh` reward 算法 + 接通 | 0.5-1d |
| **H3**(producer 状态机) | 加 log + 修 _pick_dataset coverage 逻辑 | 0.5d |
| **H4**(complementary 注入) | 加 dataset spread cap per session | 0.3d |
| **H5**(user manual) | 不需要修(orchestrator 未来会自动分散 region) | 0d |
| **H6**(战略 alpha-decay) | 加新数据源,不动选择算法 | 单独 plan |

**多重病因可能性高**:H1 + H6 + H3 联合最有可能。

---

## 5. Audit 顺序(从 cheap 到 expensive)

1. **0.1d**:5 个 SQL probe 全跑(确认 H1/H2/H3/H6)
2. **0.2d**:grep `_get_datasets_to_mine` / `_pick_dataset` / `run_dataset_weight_refresh` 代码逻辑核对
3. **0.5d**:如果 H1 确认,补 pv1 cell + 改 COALESCE 默认 → 立刻看下一个 task 的 dataset 分布是否变
4. **观察 24h**:产能是否回升到 30d 基线 ~10-50%
5. 决定:够好则停;不够则进 H2-H6

---

## 6. 不在范围

- orchestrator Phase 1 代码(已 ship,与本 bug 无关)
- 流水线 vs 串行(两路径都集中 pv1,不是路径问题)
- verdict / evaluation 阈值(FAIL 是真烂 alpha,非误判)
- 新数据源(H6 — 战略级,单独 plan)

---

## 7. 参考

- 实证 probe(2026-06-01):本对话 DB query 上文
- mining_tasks.py:701-747 `_get_datasets_to_mine`
- mining_tasks.py:1275-1288 `_pick_dataset`
- models/metadata.py:70 `mining_weight`
- [[project_dataset_steering_bandit_tierA_2026_05_22]] — bandit 设计
- [[project_dataset_cell_stats_normalization_2026_05_26]] — cell 表 cutover
- [[project_mining_generation_quality_fix_2026_05_22]] — 已识别集中价量字段问题但未真修
- [[project_depth_levers_refuted_breadth_is_answer_2026_05_25]] — 战略推论
- [[reference_competitive_analysis_v3_2026_05_26]] — 广度=数据源 / Grinold 论证
- 翻 `ENABLE_AUTO_ORCHESTRATOR` 前置:本 audit 修完 + Phase B soak
