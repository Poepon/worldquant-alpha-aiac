# Alpha Detail 页面布局重设计 — 设计文档

- **日期**: 2026-06-13
- **范围**: 重构 `frontend/src/pages/AlphaDetail.jsx` 的页面布局（结构 / 信息层级 / 决策流程），不改后端 API、不改业务语义。
- **线框图存档**: `.superpowers/brainstorm/mockups/layout-approaches.html`（A/B/C 三方案对比）、`.superpowers/brainstorm/mockups/layout-B-detailed.html`（最终方案 B 细化）

## 1. 背景与问题

当前 `AlphaDetail.jsx` 是一个约 1022 行的单文件，自上而下平铺 7 个区块：Header → Hero 指标条 → 提交决策卡 → 双列(表达式+分析 / 元数据+危机相关性) → Tabs(边际/变迁/PnL) → 人工反馈卡 → 优化 Modal。

用户反馈的四类问题（全部命中）：
1. **信息层级混乱** — 7 块卡片平铺，重点（提交决策）不够突出。
2. **一屏看不全 / 要滚动** — 关键信息分散在长页面。
3. **决策流程不顺** — 「看指标 → 判断能否提交 → 拉边际 → 提交」这条主线不连贯；尤其边际建议藏在底部 tab 里要手动点「拉取」，与顶部决策卡分离。
4. **信息冗余 / 缺失** — 部分卡片用处不大，分区不清。

页面的**核心定位（用户确认）**：决策（提交/跳过）与诊断（理解 alpha）**两者并重** —— 既要一眼能决策，又要能往下深挖，且互不淹没。

## 2. 选定方案：B —「决策侧栏 + 诊断工作区」

整体为「顶部全宽带 + 左右双栏」：

```
┌─────────────────────────────────────────────────────────┐
│ Header: ← 返回 | Alpha #id | BRAIN id | 状态 | 提交状态     │
├─────────────────────────────────────────────────────────┤
│ Hero 指标条（全宽，7 指标，可扫读的「头条证据」）            │
├──────────────────┬──────────────────────────────────────┤
│ 决策栏（吸顶常驻）  │ 诊断工作区                              │
│  · can_submit 校验 │  · 表达式（常驻顶部 + 复制）             │
│  · 边际建议(裁决)   │  · Tabs:                              │
│  · 综合分 / Margin │     [边际贡献&风险][收益曲线][详情][状态变迁]│
│  · 关键指标复述     │                                       │
│  · 提交 / 优化 按钮 │                                       │
│  · 人工反馈         │                                       │
└──────────────────┴──────────────────────────────────────┘
```

**为什么选 B**：左栏决策区 `position: sticky` 常驻视口，向下深挖右侧诊断证据时，「该不该提交」永远在眼前，最契合「两者并重」；同时天然把「结论区 / 证据区」分离，信息层级最清晰。代价是右侧内容区变窄、需做窄屏退化——已在第 5 节处理。

## 3. 分区内容规范

### 3.1 Header（全宽，一行）
- 左：返回按钮、`Alpha #{id}`、BRAIN Alpha ID（可复制 tag）、`quality_status` 状态 tag、`region · universe` tag。
- 右：提交状态 tag（`✅ 已提交 · {date}` / `⚪ 未提交`）。
- 沿用现有 `STATUS_COLORS` / `STATUS_LABELS`。

### 3.2 Hero 指标条（全宽）
- 7 个指标，顺序固定：**Sharpe · Fitness · 年化收益 · 换手率 · 最大回撤 · Margin · 自相关**。
- 取值与着色规则**完全沿用**现有 `HeroMetrics` 组件（`pickMetric` + 阈值配色 + 单位换算：returns/drawdown ×100→%，margin ×10000→bps，self_corr 取 `_self_corr`/`selfCorrelation`）。
- 缺失值显示 `—`。

### 3.3 决策栏（左栏，`position: sticky; top: <顶栏高度>`）
自上而下：
1. **can_submit 校验块** — 沿用 `CanSubmitTag` 的三态（✅可提交 / ⚠️不可提交 N 项 / 🔍未检查）+「刷新校验」按钮（`refreshCanSubmit` mutation）+ 一行文字摘要（通过项数 / 待定项数）。
2. **边际建议块（裁决）** — 大字 `SUBMIT / NEUTRAL / SKIP`（来自 `marginal.analysis`），下方 `综合 {composite_score}`、`Margin {margin_bps}bps` tag。加载状态见第 4 节。
3. **关键指标复述** — Sharpe / Margin bps / 自相关 三个小指标，使吸顶时决策自足（不必滚回去看 Hero 条）。
4. **动作按钮**（block 宽度，竖排）：
   - `⬆ 提交至 BRAIN`（primary）— 沿用现有 `submitDisabled` / `submitDisabledReason` 逻辑与 tooltip。
   - `🧪 以此为蓝本优化` — 沿用现有优化 Modal（`optimizeMutation` + budget 输入）。
5. **人工反馈块** — 👍点赞 / 👎踩（`feedbackMutation`），沿用现有「NONE 时高亮提示」逻辑与说明文案；评论展示保留。

### 3.4 诊断工作区（右栏）
- **表达式卡（常驻顶部）** — `pre` 展示 `alpha.expression` + 复制按钮。
- **Tabs（4 个，合并自现有散落区块）**：
  1. **边际贡献 & 风险** — 上：BRAIN 加入组合前后对比（沿用 `MarginalPanel`：建议 Alert + 正/负向贡献 + 7 指标前后对比卡 + 竞赛评分行 + 竞赛 ID 输入 + 「拉取」按钮）；下：**危机窗口相关性**（沿用 `CrisisCorrelationPanel`，从原右列卡片移入此处——同属「提交风险」信号）。默认激活 tab。
  2. **收益曲线** — 沿用现有 PnL 累计曲线（recharts）。
  3. **详情** — 合并原「分析」(hypothesis / logic_explanation) + 「元数据」(BRAIN id / region·universe / dataset / fields_used / operators_used / created_at)。
  4. **状态变迁** — 沿用现有 Timeline。

**做出的合并决定（用户已确认）**：
- 危机相关性 → 并入「边际贡献 & 风险」tab（提交风险信号集中）。
- 假设/逻辑 + 元数据 + 字段 + 算子 → 合成「详情」tab（用户确认不需要让「假设/逻辑」常驻表达式下方）。

## 4. 决策流程：can_submit + 边际建议加载规则

两层裁决：
- **can_submit**（BRAIN 提交前硬门）：沿用现有 `alpha.can_submit` + 手动「刷新校验」。
- **边际建议 SUBMIT/SKIP/NEUTRAL**（BRAIN before-and-after，懒加载 5–20s）：采用**智能条件拉**（用户选定）：
  - **自动触发条件**：`alpha.can_submit === true` **且** 未提交（`!alpha.date_submitted`）→ 进页后自动发起 `marginal` 拉取，决策栏边际块显示「自动计算中… ⟳」spinner，几秒后落地裁决。
  - **其它情况**（can_submit 非真 / 已提交 / 无 alpha_id）→ **不自动拉**，决策栏边际块显示「点击获取边际建议」按钮（手动触发）。
  - 拉取状态（loading / error / 结果）由父组件持有的单一 query 驱动，**决策栏的裁决摘要与「边际贡献」tab 的详细面板共享同一份数据**（沿用现有 state-lifted 模式，`marginalEnabled` 的初值改为按上述条件计算）。
  - 已提交的 alpha 仍可手动拉边际作复盘。

> 实现要点：现有 `marginalEnabled` 默认 `false`。改为：在 `alpha` 加载完成后，依条件 `useEffect` 置 `true` 触发自动拉取；其余手动入口保留。竞赛 ID 输入框、retry=false、staleTime 等保持不变。

## 5. 响应式

- **宽屏（≥ ~820px / Antd `lg`）**：双栏，左栏固定宽约 300px 且 sticky；右栏自适应。
- **窄屏（< `lg`）**：退化为单列，顺序 = Header → Hero 条 → 决策栏（回到文档流顶部，**取消 sticky**）→ 表达式 → Tabs → 反馈。即决策仍在指标之后、诊断之前，主线不变。
- Hero 指标条在窄屏按现有 `Col` 断点换行（每行 2–3 个）。

## 6. 组件拆分（顺带改善）

当前单文件 1022 行、职责混杂。借这次重构按区拆分为可独立理解/测试的组件，全部留在 `frontend/src/pages/AlphaDetail/` 目录下（或 `components/alphaDetail/`，实现时定）：

| 组件 | 职责 | 现状 |
|------|------|------|
| `AlphaDetail.jsx` | 页面骨架 + 数据 query + 布局栅格 | 瘦身为容器 |
| `HeroMetrics.jsx` | 全宽 7 指标条 | 已存在，抽出 |
| `DecisionRail.jsx` | 决策栏（can_submit + 边际裁决 + 指标复述 + 动作 + 反馈） | 新建，聚合 `CanSubmitTag` |
| `CanSubmitTag.jsx` | 可提交性三态 tag | 已存在，抽出 |
| `MarginalRiskPanel.jsx` | 「边际&风险」tab（`MarginalPanel` + `CrisisCorrelationPanel`） | 已存在，抽出+合并 |
| `CrisisCorrelationPanel.jsx` | 危机窗口相关性 | 已存在，抽出 |
| `DetailsPanel.jsx` | 「详情」tab（假设/逻辑 + 元数据 + 字段 + 算子） | 新建，合并 |
| `PnlPanel.jsx` / `TransitionsPanel.jsx` | 收益曲线 / 状态变迁 tab | 抽出 |

数据层（`useQuery` for alpha / transitions / pnl / marginal、各 mutation）保持在容器 `AlphaDetail.jsx`，通过 props 下传——保持现有 state-lifted 共享语义。

## 7. 不变量 / 不在范围内

- **不改后端**：所有 API（`getAlpha` / `getAlphaTransitions` / `getAlphaPnl` / `getAlphaMarginalContribution` / `refreshCanSubmit` / `submitAlpha` / `optimizeAlphaFromBlueprint` / `submitAlphaFeedback`）签名与返回不变。
- **不改业务语义**：指标换算、配色阈值、submit 守门逻辑、bandit/KB 等一律不动。
- **不引入新依赖**：继续用 Antd + recharts + react-query；不加 CSS 框架。
- 沿用 `glass-card` 暗色主题与现有 `utils/`（`time`、`alphaStatus`）。
- 验证方式：`cd frontend && npm run build`（项目无 eslint 配置，以 build 通过为准——见 CLAUDE.md 与记忆）。

## 8. 验收标准

1. 宽屏下决策栏 sticky 常驻，向下滚动浏览诊断 tab 时「提交决策 + 关键指标」始终可见。
2. `can_submit=true 且未提交` 的 alpha 进页后自动拉边际建议并在决策栏落地裁决；其余情况显示手动按钮，不消耗 BRAIN 配额。
3. 所有原有信息均可达（危机相关性在「边际&风险」tab，假设/逻辑/元数据/字段/算子在「详情」tab）。
4. 窄屏退化为单列且主线顺序正确。
5. `npm run build` 通过；提交/优化/反馈/刷新校验四类动作行为与改造前一致。
