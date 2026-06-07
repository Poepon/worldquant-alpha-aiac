# Pool token reservation 标定 (Phase 0, 2026-06-05)

四池解耦 plan(`docs/four_pool_decoupling_plan_2026-06-05.md` §2 成本单源)要求 HG 池的
token 预算闸用 **三段 reserve/correct**:软门 + **p95/p99 悲观预扣** + (real − reserved) 校正。
本文件记录预扣值的实测标定——Phase 0 的 `标定 p95-per-node token reservation` 交付物。

## 数据源

- 表:`llm_call_log`(`backend/models/llm_call_log.py`,G2 Phase A,2026-05-19 起)。每次
  `LLMService.call` 一行,round 边界批量 flush。`ENABLE_COST_TELEMETRY` 自 2026-05-19 在
  live DB `feature_flag_overrides` 置 true,持续记录。
- 可靠列:**`tokens_total`**(prompt+completion 合并)。`prompt_tokens`/`completion_tokens`
  仅 21/11301 行有值(OpenAI-兼容 MaaS/coding-plan 路径只回合并总数)——**勿用拆分列**。
- 窗口:2026-05-19 08:25 → 2026-06-05 12:11 UTC(≈17 天连续),11,301 行(11,058 带 token)。

## 查询

```sql
SELECT COALESCE(node_key,'<null>') AS node_key, count(*) AS n,
       percentile_cont(0.5)  WITHIN GROUP (ORDER BY tokens_total) AS p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY tokens_total) AS p95,
       percentile_cont(0.99) WITHIN GROUP (ORDER BY tokens_total) AS p99,
       max(tokens_total) AS max_tok
FROM llm_call_log
GROUP BY COALESCE(node_key,'<null>')
ORDER BY n DESC;
```

(7-day-recency 变体 `WHERE created_at > now()-interval '7 days' AND success` 与全量在噪声内一致。)

## 实测分布(tokens_total)

| node_key | n | p50 | p95 | p99 | max | Phase 1 存活? |
|---|---:|---:|---:|---:|---:|---|
| code_gen | 978 | 5447 | 12568 | 16921 | 17344 | ✅ 生成池 |
| hypothesis | 716 | 5047 | 9836 | 13780 | 17688 | ✅ 假设(HG) |
| self_correct | 729 | 2470 | 3765 | 5034 | 8499 | ✅ 生成池 |
| distill_context | 1008 | 620 | 3262 | 4513 | 6731 | ✅ HG |
| r5_alignment_c2 | 3399 | 553 | 1708 | 1881 | 3003 | ❌ Phase 1c 删 |
| r5_alignment_c1 | 3385 | 471 | 1516 | 2123 | 2985 | ❌ Phase 1c 删 |
| r1b_mutate | 474 | 1021 | 1915 | 4093 | 5123 | ❌ Phase 1c 删 |
| attribution | 437 | 654 | 1049 | 1681 | 3432 | ❌(R1a,Phase 2 重接) |
| r1b_retry | 124 | 852 | 4274 | 4851 | 6171 | ❌ Phase 1c 删 |
| llm_crossover_alpha | 51 | 1561 | 1576 | 1583 | 1583 | ❌ 已退役 |

注:`rag_query`/`validate` 无 LLM 调用故不在表中;HG 一次 cycle =
`rag_query(0) → distill_context → hypothesis → code_gen ×N → [self_correct]`。

## 落定预留值 → `config.POOL_NODE_TOKEN_RESERVE`

只为 **Phase 1 存活节点** 标定(纯前向,反馈簇 Phase 1c 删,不进池预算):

| node_key | reserve | 依据 |
|---|---:|---|
| code_gen | 17000 | p95 12568 / p99 16921;实质逼近 ~17k 输出 cap → 取 p99 做槽-悲观预扣 |
| hypothesis | 14000 | p95 9836 / p99 13780(7d p99 ~17.2k 长右尾)→ 取 p99 |
| distill_context | 4500 | p95 3262 / p99 4513 |
| self_correct | 5100 | p95 3765 / p99 5034 |
| `__default__` | 5000 | 任何未映射 LLM 节点的兜底 |

重节点(code_gen / hypothesis)用 **p99**(逼近输出 cap),轻节点用 **p99 上取整**。
校正段在调用返回后用 `real − reserved` 回写,故预扣偏保守只影响并发上界不影响计费准确性。

## 日预算 → `config.POOL_TOKEN_BUDGET_PER_DAY`

当前实测日均 burn ≈ **1.0M tokens/day**(全节点;纯前向去掉 r5/r1b/attribution 后 ≈0.85M)。
常驻池可 24/7 跑,故置 **8,000,000/day** 作 runaway backstop(≈8× 当前)——**provisional**,
Phase 1b 接线后据真实连续 burn 复标。与 `MAX_TOKENS_PER_DAY`(=500k,macro_narrative_extract
专用)**分开**,勿复用。

## 复标方法

LLM map 变更后重跑上述查询(7d-recency 变体),更新 `POOL_NODE_TOKEN_RESERVE` 与本表。
当前窗口已横跨 2026-05-31 LLM-map-ON 切换,近 7d 行已反映 live coding-plan 模型(kimi-k2.6 主导)。

**⚠️ 复标 = 改代码 + 重启,非热翻**:`POOL_TOKEN_BUDGET_PER_DAY` / `POOL_NODE_TOKEN_RESERVE`
是非 `ENABLE_` 前缀设定,`config.py` 的 `__getattribute__` flag hook 只拦 `ENABLE_` 名
(见 [[reference_feature_flag_hook_enable_prefix_only]]),故 `settings.POOL_*` 恒返回静态
Pydantic 默认、绕过 `_flag_override_cache`——写 `FeatureFlagOverride` 会静默 no-op。Phase 1b
代码须直读 `settings.POOL_*`;调参须改本文件 + config.py 默认 + 重启 worker。若日后要热调,改存
`SystemConfig` 行或专用 loader,勿走 `ENABLE_` flag hook。
