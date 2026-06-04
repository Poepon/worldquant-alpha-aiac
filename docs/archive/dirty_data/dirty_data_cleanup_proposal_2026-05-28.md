# 脏数据清理提案 — 2026-05-28

承接扫描报告 `docs/dirty_data_scan_2026-05-28_1902.md`(read-only,只数行不动库)。

下面按"污染半径"排序,每条给:**根因 → 建议动作 → 提议 SQL**。
**没有 SQL 已执行**,等你逐条勾选确认。

---

## 🔴 高优先级(确实污染 RAG / bandit 决策)

### H1. `alphas.dataset_id IS NULL` × 5,037(全部 task_id=NULL,BRAIN 当天 sync 导入)

**事实**
- 5,037 行均为 BRAIN 拉回的已提交/历史 alpha(`task_id IS NULL`),sync 时 BRAIN `settings.datasetId` 字段为空。
- memory 里 2026-05-24 `1b9e8c8` 已修 **sync_tasks 无条件覆盖 wipe** 的 bug,但 BRAIN 本来就没给 → 这批 alpha 进 RAG 池后 dataset 归属丢失。

**根因**:BRAIN 用户提交时不强制带 `datasetId`,sync 拿不到只能 NULL。

**建议**:**不要 DELETE**(这是真实 BRAIN 已提交的 alpha,RAG 召回价值高)。
方案 A(推荐)— 用 `fields_used` 反推 `dataset_id`(和 anchor backfill 同套逻辑):
```sql
-- 提议(待批准):用唯一 dataset_id 的 alpha 才回填,有冲突的留 NULL
WITH unfold AS (
  SELECT a.id, ds.dataset_id AS field_name, COUNT(*) OVER (PARTITION BY a.id) AS n
  FROM alphas a
  JOIN datafields df
    ON df.field_id = ANY(SELECT jsonb_array_elements_text(a.fields_used))
  JOIN datasets ds ON ds.id = df.dataset_id
  WHERE a.dataset_id IS NULL
    AND jsonb_typeof(a.fields_used) = 'array'
    AND jsonb_array_length(a.fields_used) > 0
),
unique_ds AS (
  SELECT id, MIN(field_name) AS ds_name
  FROM unfold
  GROUP BY id
  HAVING COUNT(DISTINCT field_name) = 1
)
UPDATE alphas a
   SET dataset_id = u.ds_name
  FROM unique_ds u
 WHERE a.id = u.id;
```
方案 B — 接受 NULL(若 `fields_used` 不全或多源),只加 metrics 标记跳过 RAG。

---

### H2. `D4 alpha_failures` 未分析 > 7 天 × 1,310(失败分析队列堆积)

**事实**
- 1,310 行 `is_analyzed=false` 超过 7 天最早回到 2026-05-19,error_type 含 `OTHER` / `QUALITY_CHECK_FAILED`。
- failure_agent 分析后会喂 RAG/distilled_logic_library;堆积意味着失败模式没回流。

**根因**:`feedback_agent` / failure-analysis beat 落后 / 偶发 crash 没补帐。

**建议**:**不要 DELETE**。两条路:
1. 标记 `is_analyzed=true` 全部"作废"(便宜,但失败学不到)。
2. 触发 feedback_agent 一次性补跑(贵)。
3. 折中:把超过 14 天的 mark `is_analyzed=true` 关闭队列,只回填最近 7 天。

```sql
-- 提议(待批准,折中):14 天前 mark analyzed=true,关闭队列
UPDATE alpha_failures
   SET is_analyzed = true
 WHERE is_analyzed = false
   AND created_at < NOW() - INTERVAL '14 days';
```

---

### H3. `D1 hypothesis` 慢性失败(alpha≥5 + pass=0)× 285

**事实**
- 285 个 hypothesis 跑了 5+ 条 alpha 一个都没过 — 这些会被 G8 cross-task forest 当 "promoted" reference,反向污染。
- 样本看 alpha_count 高达 45 / sharpe_max=NULL → 全军覆没。

**根因**:hypothesis 状态机未自动 ABANDON。

**建议**:把这些 hypothesis `is_active=false` + 写 abandon_reason。**不删行**(G8 引用依赖 id)。
```sql
-- 提议(待批准):chronic failure 退役
UPDATE hypotheses
   SET is_active = false,
       status = 'ABANDONED',
       abandon_reason = COALESCE(abandon_reason, '') || ' [auto-2026-05-28] chronic failure: alpha_count>=5 pass=0',
       updated_at = NOW()
 WHERE COALESCE(pass_count, 0) = 0
   AND COALESCE(alpha_count, 0) >= 5
   AND is_active = true;
```

---

### H4. `D3 hypothesis` is_active=true 但 abandon_reason 已写 × 290

**事实**
- V-19.7 zombie cleanup 时 abandon_reason 落了但 is_active 没拨 false → 状态机漂移。
- 取消激活才能让 G8/list_active 不再采样。

**建议**:补拨 is_active=false。
```sql
-- 提议(待批准):状态机漂移修正
UPDATE hypotheses
   SET is_active = false, updated_at = NOW()
 WHERE is_active = true
   AND (status = 'ABANDONED' OR abandon_reason IS NOT NULL);
```

---

## 🟡 中优先级(运行时残留)

### M1. `C3 experiment_runs` RUNNING > 1h × 26(永冻 task 残骸)

**事实**
- 主要是 `WATCHDOG_REVIVE` 重复触发(memory 提到 task 3737 的 6× dup-run)+ `API` 起步的 task 3729 等。
- 没有 worker 在跑 → 这些 run 永远不会结束。

**建议**:mark 已停。
```sql
-- 提议(待批准):标记孤儿 run 已终止
UPDATE experiment_runs
   SET status = 'STOPPED',
       finished_at = NOW(),
       error_message = COALESCE(error_message, '') || ' [auto-2026-05-28] orphan: marked STOPPED'
 WHERE status = 'RUNNING'
   AND COALESCE(finished_at, started_at) < NOW() - INTERVAL '1 hour';
```

### M2. `C1 mining_task` PAUSED stale × 1(task 3701 flat-session)

**事实**:2026-05-26 起 PAUSED,再没动过。无 worker 跟进。

**建议**:转 `STOPPED` 或 `EARLY_STOPPED`。
```sql
-- 提议(待批准):PAUSED 太久转 STOPPED(单条手工)
UPDATE mining_tasks SET status = 'STOPPED', updated_at = NOW() WHERE id = 3701;
```

---

## 🟢 低优先级(单点 / 不污染决策)

### L1. `B2/B4 IQC audit` 旧 schema × 1(alpha 8003,2026-05-13,missing recommendation + stale=false)

```sql
-- 提议(待批准):单点 mark stale
UPDATE alphas
   SET metrics = jsonb_set(metrics, '{_iqc_marginal,stale}', 'true'::jsonb)
 WHERE id = 8003;
```

### L2. `A4 alpha` metrics 全空 × 2(task 3094/3101,2026-05-19)

**判断**:stage='IS' status='created' → 应该 mining 中途 crash 留的占位。无人会再消费。
```sql
-- 提议(待批准):直接删
DELETE FROM alphas WHERE id IN (11833, 11838);
```

### L3. `D5 alpha_pnl` pnl IS NULL × 1,858

**判断**:看样本 `cumulative_pnl=0` 大概是 BRAIN 没回这些日子的 pnl(非交易日 / 早期数据)。先**不动**,验证一下是 BRAIN 缺还是我们写空了。建议保留作"未知缺口"。

### L4. `D8 knowledge_entries` inactive × 5,810

**判断**:`is_active=false` 本来就是软删，**不要 DELETE**(usage_count 用于历史告警);如果嫌索引大,后续考虑搬到归档表。

---

## 不动表(已确认干净)

- **B-jsonb** 所有 22 个 JSONB 列 scalar-null = 0 ✓(2026-05-28 修干净了)
- **A5 anchor-vs-fields mismatch = 0** ✓(2026-05-23 backfill 后净)
- **A1b/A2/A3/A6/B1/B3/C2/C4/C5/C6/C7/D2/D6/D7 = 0** ✓

---

## 推荐执行顺序(若批准)

1. **先做 L1+L2**(单点确认无副作用 + 删 2 个空 alpha)→ 验证脚本写入路径正常
2. **H3+H4**(hypothesis 状态机修正,影响 G8 立即可见)
3. **M1+M2**(清运行时孤儿,前端 Ops 页可立即看到改善)
4. **H1**(alphas.dataset_id 回填 — 影响 5037 行,先在副本上预演)
5. **H2**(失败队列 14d 卡线 → analyzed=true)

每一步执行前都会先 SELECT count 再 EXPLAIN 估算影响行数,确认后才 commit。

---

## 备份计划

执行前先建轻量备份(仅被改的行):
```sql
CREATE TABLE alphas_dirty_cleanup_backup_20260528 AS
  SELECT * FROM alphas WHERE id IN (… 受影响 id 列表 …);
CREATE TABLE hypotheses_dirty_cleanup_backup_20260528 AS
  SELECT * FROM hypotheses WHERE id IN (… 受影响 id 列表 …);
-- 类似 alpha_failures / experiment_runs / mining_tasks
```

回滚方案:`UPDATE … FROM backup` / `INSERT … FROM backup` 单表恢复。
