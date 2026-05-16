# Ops Dashboard 运维手册

> 日期:2026-05-16
> 适用版本:P3 ops dashboard Phase 1-4(commits `2795dab` / `96d553c` / `76ad906` / Phase 4)
> 入口:浏览器 `http://localhost:5174/ops/overview`(本地)/ 生产同域 `/ops/overview`

来源:`docs/alphagbm_skills_research_2026-05-15.md` ops dashboard 落地。9 个页面 + 28 个 endpoint,把 P0+P1+P2 daily-beat 输出全部可视化,叠加运行时 Feature Flag 控制台 + 手动 Rerun 闭环。

---

## 1. 鉴权(必读)

| 模式 | 配置 | 行为 |
|---|---|---|
| **Dev 默认** | `OPS_API_TOKEN` 未设或空 | 任何 `X-Ops-Token` 都通过(包括无 header)|
| **生产** | `OPS_API_TOKEN=<secret>` 在后端 env | 前端必须在 `localStorage["ops_token"]` 设同值,否则所有 `/api/v1/ops/*` 返 401 |

**配置生产 token**:
1. 后端启动前设环境变量(`.env` 加 `OPS_API_TOKEN=...`)
2. 操作员在浏览器 console 跑:
   ```js
   localStorage.setItem('ops_token', '<同样的 secret>')
   location.reload()
   ```
3. `/ops/feature-flags` 顶部的 "未配置 X-Ops-Token" Alert 应消失

---

## 2. 9 个页面速查

| 路由 | 用途 | 数据源 | 何时看 |
|---|---|---|---|
| `/ops/overview` | 7-beat 状态格子 + 各 region 快照 | 一次 GET 拉全 | 每次开 dashboard 第一眼 |
| `/ops/feature-flags` | ENABLE_* flag 运行时翻转 + audit | DB + Redis | 灰度发布、A/B、紧急 kill switch |
| `/ops/alpha-health` | Alpha 库 5 band 健康度 + drill-down | docs/alpha_health_check/<date>.json | 每日早 |
| `/ops/hypothesis-health` | hypothesis 触发器 + thesis_score 分布 | docs/hypothesis_health_check/<date>.json | 每日早 |
| `/ops/pillar-balance` | Five Pillars Radar + Deficit + 14d 趋势 | DB live(fresh service)/ docs 历史 | mining 池失衡告警 |
| `/ops/negative-knowledge` | Top 20 pitfall + category Pie + 30d 新增 + 禁用 Switch | DB live(KB 总是最新) | 失败 cluster 频繁时清理 KB |
| `/ops/macro-narratives` | KB 覆盖率 + 三 scope Tab + token budget | DB live + Redis 计数 | 检查 LLM token 消耗 / 覆盖盲区 |
| `/ops/regime` | 5 region 当前 regime + 14d pass_rate trend | Redis live / docs 历史 | 市场切换时 / 灰度 regime-aware flag 时 |
| `/ops/llm-op-monitor` | KB 中幻觉 op 统计 + 受影响条目 | docs/llm_op_monitor/<date>.md | 监控 LLM 输出质量漂移 |

---

## 3. 双源数据策略(每页右上角的"来源"色块)

每个数据卡片标了一个 source tag:

| Tag(色) | 含义 | 操作建议 |
|---|---|---|
| **实时**(绿) | 服务直接重新计算 | 信任,无需 Rerun |
| **今日**(蓝) | 今天的 daily beat 已落盘 `docs/<kind>/<today>.json` | 信任 |
| **历史**(黄) | 当天 beat 没跑,回退到最近一天的 archive(标 `Nd`) | 看 N 是否合理(>2 应去看 Celery beat 是否正常)|
| **缺失**(红) | 找不到任何 archive | 点页面右上 "重跑" Rerun |

**fallback 窗口**:7 天(`OPS_REPORT_ARCHIVE_FALLBACK_DAYS`)。文件大小硬上限 5MB(`OPS_REPORT_MAX_FILE_BYTES`)。mtime LRU 5 分钟缓存(`OPS_REPORT_READ_CACHE_TTL_SEC`)。

---

## 4. Feature Flag 翻转流程

适用场景:灰度某个 `ENABLE_X` 开关、紧急 kill switch、A/B 实验。

**支持的 flag(SUPPORTED_FLAGS,9 个)**:

| 分组 | Flag | 默认 |
|---|---|---|
| P0 | `ENABLE_SIGNAL_CONTROL_DUAL_RUN` | False |
| P1 | `ENABLE_GRADED_SCORE` / `ENABLE_ROBUSTNESS_CHECK` | False |
| P2-A | `ENABLE_MACRO_NARRATIVE_GUIDANCE` / `ENABLE_MACRO_NARRATIVE_EXTRACT` | False |
| P2-B | `ENABLE_PILLAR_AWARE_SELECTION` | False |
| P2-C | `ENABLE_REGIME_INFERENCE` / `ENABLE_REGIME_AWARE_THRESHOLDS` / `ENABLE_STYLE_PRESET_GUIDANCE` | False |
| P2-D | `ENABLE_NEGATIVE_KNOWLEDGE_NUDGE` | False |

**翻转步骤**:
1. 在 `/ops/feature-flags` 找到 flag,点 Switch
2. **FastAPI 进程立即生效**(写穿透 cache)
3. **Celery worker 60s 内同步**(refresher loop)
4. 若需 worker 立即感知:点 "全量刷新"(强制本进程 reload;workers 仍按 60s 节奏,但下游所有 task 立即走新值)
5. Audit Drawer 永久记录每次翻转(actor / 时间 / 备注)

**重置**:行内 "重置" 按钮 → DELETE override → 回落 env 默认。

**降级**:Redis 宕机 → 仍能从 DB 读;DB 宕机 → 回落 env 默认。系统永不锁死。

---

## 5. Rerun 节流规则

每个页面右上角的 "重跑 <task>" 按钮触发对应 Celery beat task。共享节流:

| 规则 | 值 | 触发 |
|---|---|---|
| Per-task SETNX | 60s 内同一 task 只能一次 | 第二次 → 409 Conflict + 等待秒数 toast |
| Global counter | 60s 窗口内全 ops 共 ≤10 次 | 第 11 次 → 429 Too Many Requests |
| 鉴权 | `X-Ops-Token` | 401 |

**白名单 task**:8 个(`backend.tasks.{run_alpha_health_check,run_hypothesis_health_check,run_pillar_balance_check,run_negative_knowledge_extract,run_macro_narrative_extract,run_regime_infer,monitor_llm_op_hallucinations,run_daily_feedback}`)。其他任何 task 名 → 400 Bad Request。

**触发回执**:`{task_id, accepted_at}`。task 异步执行,几秒后刷新页面看新结果。

---

## 6. 关键 endpoint cheatsheet(curl 友好)

```bash
# Feature flags
curl -H "X-Ops-Token: $T" /api/v1/ops/flags
curl -X PATCH -H "X-Ops-Token: $T" -H "Content-Type: application/json" \
  -d '{"value": true, "note": "A/B start"}' \
  /api/v1/ops/flags/ENABLE_PILLAR_AWARE_SELECTION
curl -X DELETE -H "X-Ops-Token: $T" /api/v1/ops/flags/ENABLE_PILLAR_AWARE_SELECTION/override
curl -X POST -H "X-Ops-Token: $T" /api/v1/ops/flags/refresh-all
curl -H "X-Ops-Token: $T" /api/v1/ops/flags/audit?limit=20

# Overview + 各页面 latest
curl -H "X-Ops-Token: $T" /api/v1/ops/overview
curl -H "X-Ops-Token: $T" /api/v1/ops/alpha-health/latest
curl -H "X-Ops-Token: $T" /api/v1/ops/pillar/latest
curl -H "X-Ops-Token: $T" /api/v1/ops/regime/current?region=USA

# 触发 task
curl -X POST -H "X-Ops-Token: $T" /api/v1/ops/alpha-health/rerun
curl -X POST -H "X-Ops-Token: $T" /api/v1/ops/pillar/rerun
curl -X POST -H "X-Ops-Token: $T" /api/v1/ops/tasks/trigger \
  -H "Content-Type: application/json" \
  -d '{"name": "backend.tasks.run_pillar_balance_check"}'

# 禁用一条 pitfall
curl -X PATCH -H "X-Ops-Token: $T" -H "Content-Type: application/json" \
  -d '{"is_active": false}' /api/v1/ops/negative-knowledge/entries/1234
```

---

## 7. 故障排查

| 症状 | 可能原因 | 排查 |
|---|---|---|
| 所有 `/ops/*` 401 | 设了 `OPS_API_TOKEN` 但浏览器 `localStorage.ops_token` 没设/不匹配 | console: `localStorage.getItem('ops_token')` |
| `/ops/overview` 全部 source=missing | docs 目录权限 / volume mount 错 / beat 全没跑 | `ls -la docs/{alpha_health_check,pillar_balance,regime_state}/` |
| Feature flag flip 不生效 | Cache refresher 没起 | 看 backend log `[feature_flag_runtime] async refresher started` |
| Worker flag 漂移 >60s | Celery worker 没起 refresher | 看 worker log `[feature_flag_runtime] sync refresher started`(`--pool=solo` 限制下每 worker process 一个) |
| Rerun 一直 409 | Redis SETNX 锁滞留 | 等 60s,或 `redis-cli DEL aiac:ops_trigger_throttle:<task>` |
| `/ops/macro-narratives` token budget 显示 `offline` | Redis 不可用 | 不影响其他页面;`docker compose ps redis` |
| Pillar Radar 全为零 | 7 日内无 alpha / hypothesis.pillar 全 NULL | `/ops/pillar/history?days=30` 看是否每天都零 |
| `/ops/regime` 卡片显示 cold-start | 当天 beat 未跑 + Redis 缓存空 | 点 "重跑 regime-infer";或等 10:30 SH beat |
| `/ops/llm-op-monitor` 缺失 | 06:30 SH beat 没跑 | `ls docs/llm_op_monitor/`;手动 Rerun |

---

## 8. 限制 & 已知边界

- **多 worker flag 漂移 ≤60s**:每个 Celery worker process 自维护 cache;触发 `/ops/flags/refresh-all` 只刷 FastAPI 进程
- **Pillar fresh-service 走 DB live**:大库(>10k alphas)可能 1-3s;其他页面读 docs 都是 ms 级
- **Macro Narratives token budget 默认 40k**:写死在 `frontend/src/pages/ops/MacroNarratives.jsx:DAILY_BUDGET`;改阈值需修前端
- **LLM op monitor 是 md 解析**:格式变化(新增/重命名 sections)会让 parser 静默漏字段;改任何字段名时记得跑 `pytest backend/tests/unit/test_ops_service_phase4.py`
- **没有 RBAC**:`X-Ops-Token` 单 token 模式,所有持 token 的人都能 flip flag + trigger task。如需多角色后续接 OAuth
- **PATCH /negative-knowledge/entries/{id} 直接改 KnowledgeEntry.is_active**:无 dry-run,谨慎使用(audit 在 KnowledgeEntry.updated_at 自动 bump)

---

## 9. 开发者参考

| 关注 | 位置 |
|---|---|
| Endpoint 定义 | `backend/routers/ops.py`(28 endpoints, ~600 行) |
| 业务编排 | `backend/services/ops_service.py` |
| 双源读取 | `backend/services/ops_report_reader.py` |
| Feature flag 核 | `backend/services/feature_flag_service.py` + `backend/config.py:Settings.__getattribute__` hook + `backend/feature_flag_runtime.py` |
| 单测套 | `backend/tests/unit/test_*ops*` + `backend/tests/unit/test_feature_flag_service.py` + `test_settings_flag_hook.py` + `test_pillar_service.py` |
| 集成测套 | `backend/tests/integration/test_ops_router*.py` |
| 前端入口 | `frontend/src/pages/ops/OpsLayout.jsx` |
| 共用组件 | `frontend/src/pages/ops/components/{OpsSectionCard,SourceTagBadge,RerunButton}.jsx` |
| 数据 hook | `frontend/src/pages/ops/hooks/useOpsData.js` |
| api client | `frontend/src/services/api.js`(`Ops Phase 1-4` sections, ~28 methods) |
| 侧栏接入 | `frontend/src/components/AppSidebar.jsx`(`/ops` SubMenu)|

**回归套件**:
```powershell
# 全 ops 套(Phase 1-4 总计 143 测试)
pytest backend/tests/unit/test_feature_flag_service.py `
       backend/tests/unit/test_settings_flag_hook.py `
       backend/tests/unit/test_ops_report_reader.py `
       backend/tests/unit/test_ops_service_trigger.py `
       backend/tests/unit/test_ops_service_phase2.py `
       backend/tests/unit/test_pillar_service.py `
       backend/tests/unit/test_ops_service_phase3.py `
       backend/tests/unit/test_ops_service_phase4.py `
       backend/tests/integration/test_ops_router.py `
       backend/tests/integration/test_ops_router_phase2.py `
       backend/tests/integration/test_ops_router_phase3.py `
       backend/tests/integration/test_ops_router_phase4.py

# 项目原 unit 回归(确保 ops 改动零漂移)
python backend/tests/test_suite.py --unit
```
