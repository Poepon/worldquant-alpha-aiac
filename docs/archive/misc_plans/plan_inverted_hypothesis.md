# 实施 Plan — 反向假设自动提出(flip-abandon → inverted hypothesis)

> 日期:2026-05-15
> 来源:`docs/v27_backlog.md` F 段衍生增强项
> 状态:已排期,Phase 0 待先做(确认 sampler 链路)

## 背景

V-27.92 flip-only attribution 修复(commit `9ffd242`)之后,一个连续多轮
flip-only(原方向表达式全 FAIL、符号翻转 `multiply(-1, expr)` 持续 PASS)
的 hypothesis 会被正确 ABANDONED —— 因为"原假设陈述的方向"被证伪了。

但这丢失了一个极强的 KB 信号:"这个因子/数据是有 alpha 信号的,只是方向
和原假设相反"。本增强:abandon 一个 flip-productive 的 hypothesis 时,
**自动提出它的反向假设**(statement 取反、继承因子/数据集/region)喂回
hypothesis 池。

## 调研结论(设计基于这些事实)

1. flip-only abandon 路径已存在(`persistence.py:_process_hypothesis_feedback`,
   `not real_alphas and flip_alphas` 时 attribution 硬置 `"hypothesis"`)。
2. abandon 的最终 reason 文案**区分不了** flip-only abandon vs 普通 0-PASS
   abandon —— 触发条件不能只看 reason 字符串,必须 query `hypothesis_round_stats`。
3. `hypothesis_round_stats` 表已有 `flip_alpha_count` / `flip_pass_count`
   per-round 真值列 —— 这是判断 "flip-productive" 的唯一可靠数据源。
4. `mark_abandoned` 唯一触发点在 `_process_hypothesis_feedback` 内、独立
   `_hdb` session 事务里、`_hdb.commit()` 之前(G-refine 已 V-27.B 删除)。
5. `parent_hypothesis_id` 列 + FK + relationship + `HypothesisCreateData`
   字段全都还在(V-27.B 只删了 G-refine 的写入者,没删列)。
6. 新建 `PROPOSED` + `is_active=True` + `alpha_count=0` 的行会被
   `HypothesisService.list_active` 的 `untouched_first` 排序自动排到候选
   前列 —— **sampler 不用改**(前提:Phase 0 确认)。

## 设计决策(已拍板)

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| 1 | 反向 statement 怎么生成 | **LLM 改写为主 + 模板取反 fallback** | 自由文本机械取反不可靠;abandon 低频、不在热路径;复用现成 `LLMService`(B5 v2 已在用)。LLM 失败 fallback 模板包一层,code_gen 会自己重新探索方向 |
| 2 | 自循环防护 | **新增 `Hypothesis.is_inverted` 布尔列**,触发条件硬排除 `is_inverted=True` | 反向假设自己再 flip-only 不再生成"反反假设"(等于原假设)。一次 SELECT 拦掉,比递归查 lineage 链简单可靠 |
| 3 | 触发条件 | **三条全满足**:① 最近 N 轮(`HYPOTHESIS_ABANDON_ROUNDS`=3)里 ≥ `INVERTED_HYPOTHESIS_MIN_FLIP_ROUNDS`(2)轮 flip-only;② 这些轮 `SUM(flip_pass_count)` ≥ `INVERTED_HYPOTHESIS_MIN_FLIP_PASS`(2);③ 原假设 `is_inverted=False` | 跨轮持续性才说明"方向真反了",单轮 flip pass 可能噪声。普通 0-PASS abandon(flip 全 0)不触发 |
| 4 | lineage 追踪 | **复用 `parent_hypothesis_id` + 新增 `is_inverted` 区分语义** | `parent_hypothesis_id` 答"从谁来",`is_inverted` 答"怎么来的",正交。复用零迁移成本(列已存在),migration 只加 `is_inverted` 一列 |
| 5 | 触发点 | **`_process_hypothesis_feedback` 内,`mark_abandoned` 成功后、`_hdb.commit()` 前** | 上下文都在手边(`_hdb` session / `llm_service` / `primary_hid` / flip 统计);同事务保证 abandon 与反向提出原子;整段 try/except 包死,失败不影响 abandon |
| 6 | 新假设如何进池 | `status=PROPOSED`、`is_active=True`、`alpha_count=0`;继承 region/universe/dataset_pool/key_fields/suggested_operators/target_tier/experiment_variant/kind;`parent_hypothesis_id`=原 hid;`is_inverted=True`;`confidence` 降档;建前去重(同 parent 已有 `is_inverted` 子行则跳过,保证 LangGraph replay 幂等) | `untouched_first` 排序自动选中,sampler 不用改。`experiment_variant` 必须继承否则污染 A/B 隔离 |
| 7 | kill-switch | **`INVERTED_HYPOTHESIS_ENABLED: bool = True`** + 阈值 settings,加在 `config.py` 尾部 | 跟随 `*_ENABLED` 惯例;off → 行为完全回退现状 |

## 需用户拍板

1. **sampler 链路**(Phase 0 先验证)—— plan 假设新建 `PROPOSED` 行被
   下一 task 流程自动选中。`node_hypothesis` 读了但没读到调 `list_active`
   的那段(可能在 `mining_tasks.py` / `task_service` variant 分配)。
   **若 sampler 不复用 `list_active` 的 DB 行,决策 6 不成立,plan 要加 phase。**
2. **反向假设是否跨 task 可见** —— `Hypothesis` 不强绑 task,反向假设建出
   后任何同 region task 都能 sample 到。倾向"跨 task 可见 = 正确"(符合 KB
   信号初衷),待确认。
3. **`confidence` 降档幅度** —— 建议 `medium`,可选更保守的 `low`。
4. **是否要额外效果标记** —— `is_inverted` 列够用;若想细分析"反向假设
   PASS 率 vs 普通"可能想要更多。属"不在本 plan 范围"的分析需求。

## 分阶段实施

| Phase | 内容 | 改动文件 | 工时 | 上线后行为变化 |
|---|---|---|---|---|
| **0** | 确认 sampler 链路(纯调研) | 无 | 0.5 day | 无 |
| **1** | `is_inverted` 列 + Alembic migration(`server_default='false'` 回填存量) | `models/hypothesis.py` + 新 migration | 0.5 day | 无(只加列) |
| **2** | settings(kill-switch + 2 阈值)+ 反向 statement 生成器 + `inverted_hypothesis` prompt | `config.py`、`prompts.yaml`、`loader.py`/`registry.py`、`hypothesis_service.py`(私有方法 `_generate_inverted_statement`) | 1 day | 无(无人调用) |
| **3** | service 层 `is_flip_productive_abandon` + `propose_inverted_hypothesis`(含幂等去重) | `hypothesis_service.py`(可能扩 `HypothesisCreateData` 加 `is_inverted` 字段) | 1 day | 无(无人调用) |
| **4** | 接线:`_process_hypothesis_feedback` 在 `mark_abandoned` 后触发,kill-switch + try/except + 同事务三道防护 | `persistence.py` + `backend/tests/integration/` 新集成测试 | 1 day | **唯一改运行时行为的 phase** |
| 5 | (可选)前端 `is_inverted` 标记 + lineage 展示 / 效果分析脚本 | 前端 + 脚本 | 0.5 day | — |

**合计 ~4 day**(Phase 5 可选另算)。每 phase 独立可 commit/回滚;真正有
风险的运行时改动压缩到最后一个最小 phase。

### Phase 4 集成测试必覆盖

- 连续 3 轮 flip-only(real 空、flip 有 PASS)→ 原假设 ABANDONED + 多一行
  `is_inverted=True` / `parent_hypothesis_id`=原 hid / `status=PROPOSED`,
  region/universe/dataset_pool 继承正确
- 普通 0-PASS abandon(flip 全 0)→ **不**产生反向假设
- kill-switch off → 不产生
- `is_inverted=True` 的源 abandon → 不产生(防套娃)
- replay B5 轮 → 幂等(不重复建)

## 关键风险

1. **sampler 链路未验证(最高)** —— Phase 0 必须先做。
2. **状态机是系统最绕的子系统** —— Phase 4 动 `_process_hypothesis_feedback`;
   靠 kill-switch + try/except + 同事务三道防护,前 3 phase 行为零变化。
3. **LLM 产出质量** —— 反向 statement 改写语义可能不准;有 fallback 模板,
   且 code_gen 会自己探索、反向假设也走正常 abandon 流程会被淘汰。
4. **幂等性** —— LangGraph replay B5 轮;靠 `propose_inverted_hypothesis`
   内"同 parent 已有 `is_inverted` 子行就跳过"的去重 query。
5. **依赖 `hypothesis_round_stats` flip 列准确性** —— 该表 V-27.92/71 刚修过;
   若未来 flip 拆分逻辑(`persistence.py` `_md.get("flipped")`)再变,触发
   条件受影响 —— 非本 plan 引入的耦合,但需知道。

## 不在本 plan 范围

- 反向假设效果分析(PASS 率对比)—— Phase 4 跑出数据后单独做
- 多层 lineage 可视化
- G-refine 恢复(`parent_hypothesis_id` 被本 plan 复用,`is_inverted` 正好
  区分两种衍生来源)
- 反向假设产出 PASS alpha 的 KB SUCCESS_PATTERN 特殊处理(走正常流程)
- 非 flip-only 的普通 abandon 的任何增强
- `should_abandon_hypothesis` 本身逻辑(完全不碰,只在它触发 `mark_abandoned`
  后挂副作用)
