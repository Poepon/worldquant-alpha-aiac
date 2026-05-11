# L1 Dedup Blacklist Tuning Guide

Layer 1 anti-collapse pack(2026-05-11 commit `d082178` + V-22.4 `<this commit>`)的可调参数及调参方向。

## 当前 5 个 knob(全部 `.env` 可覆盖)

| Setting | Default | 范围 | 作用 |
|---|---|---|---|
| `EXPLORE_BUDGET_PCT` | 0.3 | [0.0, 1.0] | ε-greedy 抛硬币概率:每 round 多少几率进 EXPLORE 模式(隐藏 RAG examples + 直接 novelty 指令)|
| `DEDUP_BLACKLIST_CAP` | 50 | int >= 1 | `state.recent_dedup_skeletons` FIFO 容量。超过则丢最早 |
| `DEDUP_PROMPT_T1_LIMIT` | 30 | int >= 0 | T1 prompt 渲染最近 N 个 skeleton。0 = 禁用 dedup block |
| `DEDUP_PROMPT_T2_LIMIT` | 20 | 同上 | T2 |
| `DEDUP_PROMPT_T3_LIMIT` | 15 | 同上 | T3 |

## 调参信号 — 看什么决定调哪个

### 1. **db_duplicate 率高**(SIMULATE output_data `db_duplicates` 占比 > 50%)
- 现象:LLM 仍在生成已被 dedup 拦截的形状
- 调:`DEDUP_PROMPT_T1_LIMIT` ↑(30→40),`DEDUP_BLACKLIST_CAP` ↑(50→80)
- 或:`EXPLORE_BUDGET_PCT` ↑(0.3→0.4)

### 2. **新 alpha 入库率太低**(< 1 alpha/round 平均)
- 现象:dedup 过严,LLM 推到"找不到东西"区
- 调:`DEDUP_PROMPT_T1_LIMIT` ↓(30→15),让 LLM 重新允许部分 recent skeletons
- 或:`DEDUP_BLACKLIST_CAP` ↓(50→30)
- 极端 fallback:`DEDUP_PROMPT_T1_LIMIT=0` 完全禁用 dedup signal(仅靠 ε-greedy 探索)

### 3. **Family monoculture 持续**(top family > 70%)
- 现象:dedup 拦不住 LLM 在同 family 内变体生成
- 调:`EXPLORE_BUDGET_PCT` ↑(0.3→0.5)— 更多 explore round 跳出 RAG 锚定
- 或:加 hard family-rotation 规则(代码层面,不是 config)

### 4. **Late-round LLM 输出垮**(best_sharpe 后期 → 0)
- 现象:LLM 看 30+ 黑名单 entries 后 token budget 紧
- 调:`DEDUP_PROMPT_T1_LIMIT` ↓(30→20)
- 调:`DEDUP_BLACKLIST_CAP` ↓ 也间接帮助

### 5. **想测 baseline(关 Layer 1 看对比)**
```bash
EXPLORE_BUDGET_PCT=0.0
DEDUP_PROMPT_T1_LIMIT=0
DEDUP_PROMPT_T2_LIMIT=0
DEDUP_PROMPT_T3_LIMIT=0
```
完全退回 V-22 之前行为(只保留 V-22.1 KB 写入 + V-22 BRAIN feedback)。

## .env 配置例

```env
# Layer 1 anti-collapse — current defaults
EXPLORE_BUDGET_PCT=0.3
DEDUP_BLACKLIST_CAP=50
DEDUP_PROMPT_T1_LIMIT=30
DEDUP_PROMPT_T2_LIMIT=20
DEDUP_PROMPT_T3_LIMIT=15
```

## 实测 2026-05-11 baseline 数据(参考)

post-restart 1h46m,3 workers 并发(已修):
- ε-greedy fire rate(T2 only): **22.1%**(n=86)
- dedup blacklist max size: **21** (远小于 cap 50)
- db_dup rate: 72% (down from 90% baseline)
- family diversity(T2 wrap): 跨 3 family

post-Redis-lock(单 worker)+ T1 phase 50min:
- T1 STRATEGY_SELECT n=5, fire rate 20.0%
- T1 PASS rate: 0/65 (但有 BRAIN auth 干扰,数据不可信)
- T1 rationale family: SENTIMENT, OPTION, FACTOR_COMPOSITE, PRICE_PV, FUNDAMENTAL, MICROSTRUCTURE(6 family)

## 实验建议

跑 A/B 对比时:
1. 先用 default 跑 1 个完整 cascade round 拿 baseline
2. 改一个 knob,跑 1 个 cascade round
3. 比较 db_dup_rate / PASS_count / family_diversity
4. 调下个 knob

不要一次调多个 knob,会无法归因。

## 当前状态评估(2026-05-11)

| 指标 | 推断 | 行动 |
|---|---|---|
| db_dup 72% | 仍偏高 | T1_LIMIT 30→40 试试 |
| PASS rate ≈ 0 | 太低,但 BRAIN auth bug 在 04:00 才修(649ae92)| 等下一轮 cascade 看数据 |
| Family 多样 | 已 6 family | 不动 |
| Explore fire rate 20-22% | 接近 30% 目标 | 不动 |

**暂不主动调**;等 1-2 个完整 cascade round(auth fix 生效后)拿干净数据再决定。
