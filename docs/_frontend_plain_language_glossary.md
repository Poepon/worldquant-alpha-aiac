# 前端术语通俗化映射表（SSOT）

目标：把前端界面上**展示给人看的**工程内部黑话/英文代号，**完全替换为通俗易懂的中文**，让不懂这套系统内部实现的人也能看懂界面。

本表是所有改写工作的唯一权威标准 —— 同一个术语在所有页面必须译成同一个中文，避免各页打架。

---

## 改写规则（务必遵守）

1. **只改"展示文本"**：JSX 文本节点、`title`/`label`/`header`/`placeholder`/`Tooltip` 内容、`Tag`/`Statistic title`/`Button` 文字、`Alert` 的 message/description、`message.success(...)` 等给用户看的提示语。
2. **绝不改**：变量名、函数名、`dataIndex`/`key`/`queryKey`、API 字段名、`colorMap`/`ORDER` 等常量的 **key**、import、注释里的英文（注释不是展示文本，但若顺手可改善则可改）、CSS。
3. **后端返回值当 key 用、又直接显示给用户**的情况（如 `<Tag>{s}</Tag>` 里 `s='PENDING_SIM'`）：**不要改 key**，而是**新增一个 label 映射**（如 `const STAGE_LABEL = { PENDING_SIM: '排队待回测', ... }`），显示时用 `STAGE_LABEL[s] || s`。
4. **保留的行业标准术语**（不翻译，但确保旁边有中文说明）：`alpha`（产品核心概念，全系统统一保留）、`Sharpe`、`Fitness`、`Turnover`/换手率、`Margin`、`bps`、`IC`、`BRAIN`、`token`。这些是量化/平台行业通用词，用户本人也这么叫。
5. **数值阈值原样保留**（0.7、5bps、500、30% 等）。
6. 不破坏 JSX 结构、不改组件逻辑、不动样式。改完该编译通过。
7. 句子里混着代号的（如"fetch_cross_task_promoted 的实际池子"），**理解含义后重写整句**为通顺中文，而非逐词查表。

---

## 术语映射

### 挖掘流水线三阶段（最高频）
| 代号 | 大白话 |
|------|--------|
| 挖掘池 (HG/S/E) | 挖掘流水线（想法生成 → 回测 → 评估） |
| HG / hg / hg_loop / 生成阶段 | 想法生成 |
| S / s / s_loop / 模拟阶段 | 回测模拟 |
| E / e / e_loop / 评估阶段 | 评估入库 |
| HG → S → E | 想法生成 → 回测 → 评估入库 |
| pool / 池 | 流水线 / 处理环节（按语境） |

### 队列
| 代号 | 大白话 |
|------|--------|
| hyp_intent | 想法队列（待挖掘的想法） |
| candidate_queue | 候选队列（待回测/评估的候选 alpha） |

### 队列状态码（用 label 映射，勿改 key）
| 代号 | 大白话 |
|------|--------|
| PENDING | 排队中 |
| PENDING_SIM | 排队待回测 |
| SIMULATING | 回测中 |
| PENDING_EVAL | 排队待评估 |
| EVALUATING | 评估中 |
| CLAIMED | 已认领（处理中） |
| DONE | 已完成 |
| FAILED | 失败 |
| PURGED | 已清除 |

### 工作进程 / 调度
| 代号 | 大白话 |
|------|--------|
| worker | 工作进程 |
| supervisor | 进程守护 |
| beat / daily-beat | 定时任务 |
| scheduler | 调度器 |
| lease / 超 lease / past-lease | 处理超时（任务认领后超时未完成） |
| 心跳 / heartbeat | 心跳（保留） |
| crash-loop | 反复崩溃 |
| drain | 暂停接新活（软停） |
| resume | 恢复 |
| in-flight / 在飞 | 处理中 |
| cutover | 切换上线 |
| expected / expected_workers | 应有数量 |
| recycle / lease-recycle | 超时回收 |

### BRAIN 回测
| 代号 | 大白话 |
|------|--------|
| sim / simulate | 回测 |
| 共享 sim 槽 / slot | 并发回测名额 |
| concurrent_sims | 正在回测数 |
| budget | 配额 |
| budget_sims_today | 今日回测次数 |
| budget_tokens_today | 今日 token 用量 |
| throughput | 吞吐（产出速度） |

### 提交相关
| 代号 | 大白话 |
|------|--------|
| can_submit | 可提交 |
| self_corr / self-correlation / SELF_CORRELATION | 与已提交策略的相关度 |
| IQC / IQC Δscore | 竞赛评分变化（IQC 竞赛） |
| Δscore / delta_score | 评分变化 |
| ΔSharpe | Sharpe 增量 |
| re-sim | 重新回测 |
| sub-universe / subuniv | 子股票池 |
| orthogonality | 差异度（与现有策略的不重复程度） |
| novelty | 新颖度 |
| backlog / 积压 | 待提交积压 |
| yield / 产率 | 产出率 |
| marginal | 边际贡献 |
| robustness verdict / ROBUST/MODERATE/FRAGILE | 稳健性结论（稳健 / 一般 / 脆弱） |
| is.checks | BRAIN 提交前检查 |
| PROD-corr | 与生产策略的相关度 |
| 近门槛 | 接近门槛 |
| degrade-open | 降级放行 |
| unknown-op | 未知算子 |

### 质量状态（alpha quality_status）
| 代号 | 大白话 |
|------|--------|
| PASS | 通过 |
| PASS_PROVISIONAL | 暂定通过 |
| OPTIMIZE | 待优化 |
| FAIL | 未通过 |
| GREEN / ORANGE / RED（健康 band） | 健康 / 注意 / 异常 |
| band | 健康档位 |

### 知识库 / RAG / 假设
| 代号 | 大白话 |
|------|--------|
| RAG | 知识检索 |
| CoSTEER | 知识库与检索 |
| R8（遥测） | （去掉代号，写"知识检索遥测"） |
| pillar / 支柱 | 因子类别 |
| 五支柱 | 五大因子类别 |
| hypothesis / Hypothesis | 假设 |
| node_hypothesis | 假设节点 |
| LEVEL-0 | （重写，指"不跨任务复用"，按语境写"当前模式不晋升复用"） |
| PROMOTED / promote / 晋升 | 提升复用 |
| ACTIVE | 生效中 |
| negative knowledge | 失败经验库 |
| pitfall | 教训 / 坑 |
| category | 类别 |
| decay / decayed / 衰减 | 过期 / 已过期 |
| distill / 蒸馏 | 提炼 |
| hallucination / 幻觉 | 虚构算子（LLM 编造的不存在算子） |
| fetch_cross_task_promoted | 跨任务复用提取（重写整句） |
| macro narratives / 宏观叙事 | 宏观叙事（保留，已通俗） |
| Mechanism | 机制 |
| Scope / field_id | 范围 / 字段 |

### 市场行情
| 代号 | 大白话 |
|------|--------|
| regime | 市场行情阶段 |
| regime 转 / 转向 | 行情切换 |
| IS / In-Sample | 样本内 |
| OS / Out-Sample | 样本外 |
| rolling test_period | 滚动测试区间 |
| 老边际恢复 / 赢家 | 老策略优势恢复 / 历史优胜策略 |

### LLM / 系统配置
| 代号 | 大白话 |
|------|--------|
| node_key | 功能模块 |
| 功能块 | 功能模块 |
| thinking_effort / high/low | 推理强度（高/低） |
| token-plan / 构造默认端点 | 默认网关（重写说明） |
| legacy / byte-for-byte legacy | 旧版默认 |
| Feature Flag | 功能开关 |
| Flag | 开关 |
| provider / 厂商 | 模型厂商（保留"厂商"） |
| code_gen | 代码生成 |
| hypothesis（功能块名） | 假设生成 |
| Caller | 调用方 |
| node | 节点 / 环节 |

### BRAIN 角色
| 代号 | 大白话 |
|------|--------|
| CONSULTANT 模式 | 顾问模式 |
| USER 模式 | 普通用户模式 |
| multi-sim | 批量回测 |
| testPeriod | 测试区间 |
| region / region_count | 地区 / 可用地区数 |
| 403 | 权限被拒（403） |

### 统计/对账
| 代号 | 大白话 |
|------|--------|
| pairwise | 两两相关 |
| Max/Median/Mean pairwise | 最大/中位/平均两两相关 |
| Hotspots | 高相关热点 |
| watermark | 处理进度位置 |
| denorm | 汇总预计算 |
| attribution / AttributionType | 归因 / 归因类型 |
| Phase 2 | 第二阶段 |
| grace_sec | 宽限秒数 |
| window_days | 统计窗口天数 |
| skew | 偏斜 |
| Deficit | 缺口 |
| shares | 占比 |
| stamped / legacy inferred | 已标注 / 旧数据推断 |
| phase（R13 因子）| 阶段 |
| 天龄 | 入库天数 |
| 过期（stale）| 已过期 |
| AST 原创性 | 代码结构相似度去重 |
| Shadow 校准 / sweet spot | 影子模式校准 / 最佳区间 |
| τ (tau) | 阈值 τ（保留符号，加"阈值"说明） |

### 缓存
| 代号 | 大白话 |
|------|--------|
| 缓存命中 / hit | 缓存命中（保留，通俗） |
| 复用 | 复用（保留） |
| R9 cache | 回测结果缓存 |
| 模拟缓存 | 回测缓存 |

---

## 注意：菜单（AppSidebar.jsx）也要改
- `挖掘池 (HG/S/E · 总览/队列/工作器)` → `挖掘流水线（想法生成/回测/评估 · 总览/队列/工作进程）`
- `Regime 转向监测` → `行情切换监测`
- `AST 原创性` → `代码结构去重`
- `假设森林` → 保留（已通俗）/ 或"假设库分布"
- `失败模式沉淀` → `失败经验库`
- `模拟缓存 (R9)` → `回测缓存`
- `知识库与 RAG` → `知识库与检索`
- `池认知对账 (Phase 2)` → `知识库对账（第二阶段）`
- `LLM 算子监控` → `LLM 算子监控`（保留，"算子"是量化术语）
- `Hypothesis 池漏斗` → `假设队列漏斗`
- `Feature Flag` → `功能开关`
- `BRAIN 模式` → `BRAIN 账号模式`
- `提交产率 (yield)` → `提交产出率`
- `优化 sweep 审计` → `参数优化审计`
- `[归档] 逻辑库 / 语法校验` → `[归档] 逻辑库 / 语法校验`（归档项可保留）
