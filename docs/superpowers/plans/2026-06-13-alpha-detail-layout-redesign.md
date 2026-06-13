# Alpha Detail 页面布局重设计 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `frontend/src/pages/AlphaDetail.jsx`（约 1022 行单文件）重构为方案 B 布局 —— 顶部全宽(Header+指标条) + 左侧吸顶决策栏 + 右侧诊断工作区(表达式常驻 + 4 tab)，并拆成可独立维护的子组件。

**Architecture:** 纯前端重构，**不改任何后端 API、不改业务语义/配色/换算**。先把现有代码原样抽到 `pages/AlphaDetail/` 目录下的子组件（行为零变化、build 把关），再重排布局栅格、加 sticky 与响应式，最后把边际建议改为「智能条件拉」。数据 query/mutation 全部留在容器 `index.jsx`，经 props 下传，保持现有 state-lifted 共享语义。

**Tech Stack:** React 18 + Vite + Ant Design (Row/Col/Card/Tabs/Statistic/Descriptions/Timeline) + recharts + @tanstack/react-query。

**测试约定（重要）：** 本仓库前端**无单测框架（无 vitest/jest）、无 eslint 配置**。按 CLAUDE.md 与既有约定，**每个任务的验证 = `cd frontend && npm run build` 必须通过**（替代 TDD 单测；不擅自引入测试/lint 工具）。末尾加一次人工冒烟。每步改动小、build 绿即提交。

**设计文档：** `docs/superpowers/specs/2026-06-13-alpha-detail-layout-redesign-design.md`
**线框图：** `.superpowers/brainstorm/mockups/layout-B-detailed.html`

---

## File Structure

重构后 `frontend/src/pages/AlphaDetail/` 目录（`index.jsx` 让 `App.jsx` 现有 `import AlphaDetail from './pages/AlphaDetail'` 免改）：

| 文件 | 职责 |
|------|------|
| `pages/AlphaDetail/index.jsx` | 页面容器：所有 `useQuery`/`useMutation`/state、布局栅格(Header + Hero + 双栏)、Modal |
| `pages/AlphaDetail/HeroMetrics.jsx` | 全宽 7 指标条（含 `pickMetric` 工具） |
| `pages/AlphaDetail/CanSubmitTag.jsx` | 可提交性三态 tag |
| `pages/AlphaDetail/DecisionRail.jsx` | 左决策栏：can_submit + 边际裁决 + 指标复述 + 提交/优化动作 + 人工反馈 |
| `pages/AlphaDetail/CrisisCorrelationPanel.jsx` | 危机窗口相关性（含 `CRISIS_WINDOW_LABELS`） |
| `pages/AlphaDetail/MarginalRiskPanel.jsx` | 「边际&风险」tab：现有 `MarginalPanel` 详细面板 + `CrisisCorrelationPanel` |
| `pages/AlphaDetail/DetailsPanel.jsx` | 「详情」tab：假设/逻辑 + 元数据 + 字段 + 算子 |
| `pages/AlphaDetail/PnlPanel.jsx` | 「收益曲线」tab |
| `pages/AlphaDetail/TransitionsPanel.jsx` | 「状态变迁」tab |

> 注：旧文件 `pages/AlphaDetail.jsx` 在 Task 1 删除（内容迁入 `pages/AlphaDetail/index.jsx`）。Windows 下大小写不敏感，`AlphaDetail.jsx`(文件) 与 `AlphaDetail/`(目录) 不能并存——Task 1 必须一次性完成「建目录 + 删旧文件」。

---

## Task 1: 建目录骨架（行为零变化的纯搬迁）

把现有单文件原样搬到 `pages/AlphaDetail/index.jsx`，先不拆子组件、不改任何逻辑。确保导入与构建不破。

**Files:**
- Create: `frontend/src/pages/AlphaDetail/index.jsx`
- Delete: `frontend/src/pages/AlphaDetail.jsx`

- [ ] **Step 1: 复制旧文件为新目录入口**

```bash
cd "E:/WorldQuant/worldquant-alpha-aiac/frontend"
mkdir -p src/pages/AlphaDetail
git mv src/pages/AlphaDetail.jsx src/pages/AlphaDetail/index.jsx
```

- [ ] **Step 2: 修正相对导入深度**

`index.jsx` 现在深了一层。把文件内所有 `from '../` 改为 `from '../../`（共 3 处：`../services/api`、`../utils/time`、`../utils/alphaStatus`）。

```diff
- import api from '../services/api'
- import { formatRelative, formatDateTime } from '../utils/time'
- import { STATUS_COLORS, STATUS_LABELS } from '../utils/alphaStatus'
+ import api from '../../services/api'
+ import { formatRelative, formatDateTime } from '../../utils/time'
+ import { STATUS_COLORS, STATUS_LABELS } from '../../utils/alphaStatus'
```

- [ ] **Step 3: build 验证**

Run: `cd "E:/WorldQuant/worldquant-alpha-aiac/frontend" && npm run build`
Expected: 构建成功（`✓ built in ...`），无 "Could not resolve" 报错。`App.jsx` 的 `import AlphaDetail from './pages/AlphaDetail'` 因目录含 `index.jsx` 自动解析。

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "refactor(alpha-detail): 单文件迁入 pages/AlphaDetail/index.jsx(零行为变化)"
```

---

## Task 2: 抽出 HeroMetrics + pickMetric

把已存在的 `HeroMetrics` 组件与其依赖的 `pickMetric` 工具抽成独立文件。

**Files:**
- Create: `frontend/src/pages/AlphaDetail/HeroMetrics.jsx`
- Modify: `frontend/src/pages/AlphaDetail/index.jsx`

- [ ] **Step 1: 新建 HeroMetrics.jsx**

把 `index.jsx` 中现有的 `pickMetric` 函数（原 62-70 行）与 `HeroMetrics` 组件（原 181-249 行）**原样**剪切到新文件，补上 imports 与导出。`pickMetric` 同时具名导出（后续 DecisionRail 复用）。

```jsx
import { Row, Col, Card, Statistic, Tooltip as AntTooltip } from 'antd'

// Pull a numeric metric, preferring alpha.metrics then is_metrics. Returns null
// for missing / NaN so renderers can show an em-dash instead of "NaN".
export function pickMetric(alpha, key) {
  const m = alpha?.metrics || {}
  const ism = alpha?.is_metrics || {}
  const a = m[key]
  if (typeof a === 'number' && !Number.isNaN(a)) return a
  const b = ism[key]
  if (typeof b === 'number' && !Number.isNaN(b)) return b
  return null
}

// ↓↓↓ 原 index.jsx 中 HeroMetrics 函数体原样粘贴（181-249 行），不改任何逻辑/配色/换算 ↓↓↓
export default function HeroMetrics({ alpha }) {
  // ... 原样内容 ...
}
```

- [ ] **Step 2: 在 index.jsx 删除被抽出的代码并改为导入**

删除 `index.jsx` 里的 `pickMetric` 与 `HeroMetrics` 定义；在文件顶部 import 区加：

```jsx
import HeroMetrics from './HeroMetrics'
```

（`index.jsx` 自身若仍直接用到 `pickMetric` 则改为 `import HeroMetrics, { pickMetric } from './HeroMetrics'`；检查后按需保留。）

- [ ] **Step 3: build 验证**

Run: `cd "E:/WorldQuant/worldquant-alpha-aiac/frontend" && npm run build`
Expected: 成功，无未定义符号报错。

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "refactor(alpha-detail): 抽出 HeroMetrics + pickMetric"
```

---

## Task 3: 抽出 CanSubmitTag 与 CrisisCorrelationPanel

**Files:**
- Create: `frontend/src/pages/AlphaDetail/CanSubmitTag.jsx`
- Create: `frontend/src/pages/AlphaDetail/CrisisCorrelationPanel.jsx`
- Modify: `frontend/src/pages/AlphaDetail/index.jsx`

- [ ] **Step 1: 新建 CanSubmitTag.jsx**

把现有 `CanSubmitTag` 组件（原 131-174 行）原样移入，补 imports：

```jsx
import { Tag, Tooltip as AntTooltip } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'

export default function CanSubmitTag({ canSubmit, failed, pending, loading, onRefresh }) {
  // ... 原样内容 ...
}
```

- [ ] **Step 2: 新建 CrisisCorrelationPanel.jsx**

把现有 `CRISIS_WINDOW_LABELS` 常量（原 53-58 行）与 `CrisisCorrelationPanel` 组件（原 72-129 行）原样移入：

```jsx
import { Space, Tag, Typography, Tooltip as AntTooltip } from 'antd'

const { Text } = Typography

const CRISIS_WINDOW_LABELS = {
  covid_2020: 'COVID 2020',
  rate_shock_2022: '利率冲击 2022',
  svb_2023: 'SVB 2023',
  tariff_2025: '关税 2025',
}

export default function CrisisCorrelationPanel({ crisis }) {
  // ... 原样内容 ...
}
```

- [ ] **Step 3: index.jsx 删除被抽代码并导入**

删除 `index.jsx` 里这三个定义；加导入：

```jsx
import CanSubmitTag from './CanSubmitTag'
import CrisisCorrelationPanel from './CrisisCorrelationPanel'
```

- [ ] **Step 4: build 验证**

Run: `cd "E:/WorldQuant/worldquant-alpha-aiac/frontend" && npm run build`
Expected: 成功。

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "refactor(alpha-detail): 抽出 CanSubmitTag + CrisisCorrelationPanel"
```

---

## Task 4: 抽出 MarginalRiskPanel / PnlPanel / TransitionsPanel / DetailsPanel（建 4 个 tab 内容组件）

把现有 tab 内容拆成独立组件。`MarginalRiskPanel` = 现有 `MarginalPanel`（原 854-1022 行）+ 末尾追加 `CrisisCorrelationPanel`。`DetailsPanel` 是「分析卡 + 元数据卡」合并的新组件。

**Files:**
- Create: `frontend/src/pages/AlphaDetail/MarginalRiskPanel.jsx`
- Create: `frontend/src/pages/AlphaDetail/PnlPanel.jsx`
- Create: `frontend/src/pages/AlphaDetail/TransitionsPanel.jsx`
- Create: `frontend/src/pages/AlphaDetail/DetailsPanel.jsx`
- Modify: `frontend/src/pages/AlphaDetail/index.jsx`

- [ ] **Step 1: 新建 MarginalRiskPanel.jsx**

把现有 `MarginalPanel`（原 854-1022 行）原样移入并改名 `MarginalRiskPanel`，新增 `crisis` prop，在返回 JSX 末尾（`</div>` 收尾前）追加危机相关性小节：

```jsx
import { useState } from 'react'
import {
  Row, Col, Card, Typography, Tag, Button, Space, Descriptions, Spin, Empty, Input, Alert, Divider,
} from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import CrisisCorrelationPanel from './CrisisCorrelationPanel'

const { Text } = Typography

export default function MarginalRiskPanel({
  alpha, marginal, loading, error, enabled, competition, setCompetition, onFetch, crisis,
}) {
  const analysis = marginal?.analysis
  return (
    <div>
      {/* ↓ 原 MarginalPanel 内全部内容原样保留（Alert/输入/拉取按钮/前后对比卡…） ↓ */}
      {/* ... 原样 ... */}

      {/* ↓ 新增：危机窗口相关性小节（从原右列卡片迁来） ↓ */}
      <Divider style={{ margin: '20px 0 12px' }} />
      <Space style={{ marginBottom: 10 }}>
        <Text strong>危机窗口相关性</Text>
        <Tag color="purple">压力测试 · 隐性集中度风险</Tag>
      </Space>
      <CrisisCorrelationPanel crisis={crisis} />
    </div>
  )
}
```

- [ ] **Step 2: 新建 PnlPanel.jsx**

把现有「收益曲线」tab 的 children（原 724-745 行，含 loading/empty/ResponsiveContainer 折线图）封装成组件，输入已算好的 `pnlData` 与 `loading`：

```jsx
import { Spin, Empty } from 'antd'
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts'

export default function PnlPanel({ pnlData, loading }) {
  if (loading) return <Spin />
  if (!pnlData || pnlData.length === 0) {
    return <Empty description="尚无 PnL 数据（挖掘 / 同步命中本地缓存后落库）" />
  }
  return (
    <ResponsiveContainer width="100%" height={320}>
      {/* ↓ 原折线图 JSX 原样 ↓ */}
    </ResponsiveContainer>
  )
}
```

- [ ] **Step 3: 新建 TransitionsPanel.jsx**

把现有「状态变迁」tab children（原 678-712 行）封装，输入 `transitions` 与 `loading`：

```jsx
import { Spin, Empty, Timeline, Tag, Typography, Space, Tooltip as AntTooltip } from 'antd'
import { formatRelative, formatDateTime } from '../../utils/time'
import { STATUS_COLORS, STATUS_LABELS } from '../../utils/alphaStatus'

const { Text } = Typography

export default function TransitionsPanel({ transitions, loading }) {
  if (loading) return <Spin />
  if (!transitions || transitions.length === 0) return <Empty description="尚无状态变迁记录" />
  return (
    <Timeline
      items={transitions.map((t) => ({ /* ↓ 原 map 体原样 ↓ */ }))}
    />
  )
}
```

- [ ] **Step 4: 新建 DetailsPanel.jsx（合并「分析」+「元数据」）**

合并原「分析」卡（hypothesis/logic_explanation，原 562-581 行）与「元数据」卡（原 585-620 行）为一个 tab 内容；用 `Divider` 分隔两段：

```jsx
import { Typography, Descriptions, Space, Tag, Divider, Tooltip as AntTooltip } from 'antd'
import { formatRelative, formatDateTime } from '../../utils/time'

const { Text, Paragraph } = Typography

export default function DetailsPanel({ alpha }) {
  return (
    <div>
      {(alpha.hypothesis || alpha.logic_explanation) && (
        <>
          {alpha.hypothesis && (
            <>
              <Text strong>假设 (Hypothesis):</Text>
              <Paragraph style={{ color: 'rgba(255,255,255,0.85)' }}>{alpha.hypothesis}</Paragraph>
            </>
          )}
          {alpha.logic_explanation && (
            <>
              <Text strong>逻辑解释:</Text>
              <Paragraph style={{ color: 'rgba(255,255,255,0.85)' }}>{alpha.logic_explanation}</Paragraph>
            </>
          )}
          <Divider />
        </>
      )}
      <Descriptions column={1} size="small">
        <Descriptions.Item label="BRAIN Alpha ID">
          {alpha.alpha_id
            ? <Text code copyable={{ text: alpha.alpha_id, tooltips: ['复制', '已复制'] }}>{alpha.alpha_id}</Text>
            : <Text type="secondary">未提交至 BRAIN</Text>}
        </Descriptions.Item>
        <Descriptions.Item label="地区 / 股票池">{alpha.region} · {alpha.universe}</Descriptions.Item>
        <Descriptions.Item label="数据集">{alpha.dataset_id || '—'}</Descriptions.Item>
        <Descriptions.Item label="使用字段">
          <Space wrap size={[4, 4]}>
            {(alpha.fields_used || []).length ? alpha.fields_used.map((f) => <Tag key={f}>{f}</Tag>) : '—'}
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="使用算子">
          <Space wrap size={[4, 4]}>
            {(alpha.operators_used || []).length ? alpha.operators_used.map((o) => <Tag key={o} color="blue">{o}</Tag>) : '—'}
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="创建时间">
          <AntTooltip title={formatDateTime(alpha.created_at)}><span>{formatRelative(alpha.created_at)}</span></AntTooltip>
        </Descriptions.Item>
      </Descriptions>
    </div>
  )
}
```

- [ ] **Step 5: index.jsx 暂接新组件（保持旧布局，仅替换 tab children）**

此步**先不动整体布局**，只把现有 `Tabs.items` 的 children 换成新组件、删掉旧「分析/元数据/危机」卡片定义，确保等价。在 `index.jsx`：
- 加导入：

```jsx
import MarginalRiskPanel from './MarginalRiskPanel'
import PnlPanel from './PnlPanel'
import TransitionsPanel from './TransitionsPanel'
import DetailsPanel from './DetailsPanel'
```

- 把 marginal tab 的 `<MarginalPanel .../>` 改为 `<MarginalRiskPanel ... crisis={metrics?._crisis_correlations} />`；pnl/transitions tab children 改用 `<PnlPanel pnlData={pnlData} loading={pnlLoading} />`、`<TransitionsPanel transitions={transitions} loading={transLoading} />`。
- 删除底部原 `MarginalPanel` 函数定义（已迁出）。

- [ ] **Step 6: build 验证**

Run: `cd "E:/WorldQuant/worldquant-alpha-aiac/frontend" && npm run build`
Expected: 成功。此时页面外观仍≈旧版（布局未变），但 tab 内容来自新组件、危机相关性已并入边际 tab。

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "refactor(alpha-detail): tab 内容拆为 4 个面板组件 + 危机相关性并入边际tab"
```

---

## Task 5: 建 DecisionRail（左决策栏组件）

把「提交决策卡」「人工反馈卡」的内容重组为一个左栏组件：can_submit 校验 → 边际裁决摘要 → 关键指标复述(Sharpe/Margin bps/自相关) → 提交/优化按钮 → 人工反馈。所有动作经 props 回调，组件本身无 query/mutation。

**Files:**
- Create: `frontend/src/pages/AlphaDetail/DecisionRail.jsx`
- Modify: `frontend/src/pages/AlphaDetail/index.jsx`

- [ ] **Step 1: 新建 DecisionRail.jsx**

```jsx
import { Card, Typography, Tag, Button, Space, Statistic, Divider, Tooltip as AntTooltip } from 'antd'
import {
  ReloadOutlined, CloudUploadOutlined, ExperimentOutlined, LikeOutlined, DislikeOutlined,
} from '@ant-design/icons'
import CanSubmitTag from './CanSubmitTag'
import { pickMetric } from './HeroMetrics'

const { Text } = Typography

const REC_COLOR = { SUBMIT: 'success', SKIP: 'error', NEUTRAL: 'warning' }

export default function DecisionRail({
  alpha,
  metrics,
  analysis,
  marginalLoading,
  marginalAutoPending,   // true=已自动触发但结果未到 → 显示「计算中」
  onFetchMarginal,       // 手动拉取（非自动场景）
  refreshLoading,
  onRefreshCanSubmit,
  submitDisabled,
  submitDisabledReason,
  submitLoading,
  onSubmit,
  optimizeLoading,
  onOpenOptimize,
  feedbackLoading,
  onFeedback,
}) {
  const selfCorr =
    typeof metrics._self_corr === 'number' ? metrics._self_corr
      : typeof metrics.selfCorrelation === 'number' ? metrics.selfCorrelation : null
  const sharpe = pickMetric(alpha, 'sharpe')
  const margin = pickMetric(alpha, 'margin')
  const marginBps = margin == null ? null : margin * 10000

  return (
    <div style={{ position: 'sticky', top: 16 }}>
      <Card className="glass-card" title={<Space><CloudUploadOutlined />提交决策</Space>}>
        {/* 1) can_submit 校验 */}
        <Space wrap size={8}>
          <CanSubmitTag
            canSubmit={alpha.can_submit}
            failed={metrics._brain_failed_checks || []}
            pending={metrics._brain_pending_checks || []}
            loading={refreshLoading}
            onRefresh={onRefreshCanSubmit}
          />
          <Button size="small" icon={<ReloadOutlined />} loading={refreshLoading} onClick={onRefreshCanSubmit}>
            刷新校验
          </Button>
        </Space>

        <Divider style={{ margin: '12px 0' }} />

        {/* 2) 边际裁决 */}
        <div className="label" style={{ fontSize: 11, color: '#8a93a6', marginBottom: 6 }}>边际建议</div>
        {analysis ? (
          <Space direction="vertical" size={6} style={{ width: '100%' }}>
            <Tag color={REC_COLOR[analysis.recommendation] || 'default'} style={{ fontSize: 16, padding: '4px 12px' }}>
              {analysis.label}
            </Tag>
            <Space wrap size={6}>
              {analysis.composite_score != null && (
                <Tag color={analysis.composite_score > 0 ? 'green' : analysis.composite_score < 0 ? 'red' : 'default'}>
                  综合 {analysis.composite_score > 0 ? '+' : ''}{analysis.composite_score}
                </Tag>
              )}
              {analysis.margin_bps != null && (
                <Tag color={analysis.margin_bps < 0 ? 'red' : analysis.margin_bps < 5 ? 'orange' : 'blue'}>
                  Margin {analysis.margin_bps}bps
                </Tag>
              )}
            </Space>
            <Text type="secondary" style={{ fontSize: 11 }}>详见右侧「边际贡献 &amp; 风险」</Text>
          </Space>
        ) : marginalLoading || marginalAutoPending ? (
          <Space><ReloadOutlined spin /><Text type="secondary" style={{ fontSize: 12 }}>BRAIN 计算中（5-20s）…</Text></Space>
        ) : (
          <Button size="small" icon={<ReloadOutlined />} disabled={!alpha?.alpha_id} onClick={onFetchMarginal}>
            获取边际建议
          </Button>
        )}

        <Divider style={{ margin: '12px 0' }} />

        {/* 3) 关键指标复述（吸顶自足） */}
        <Space size={24}>
          <Statistic title="Sharpe" value={sharpe == null ? '—' : sharpe} precision={sharpe == null ? undefined : 2}
            valueStyle={{ fontSize: 18, color: sharpe == null ? undefined : sharpe >= 1.5 ? '#3f8600' : sharpe >= 1.0 ? '#d48806' : '#cf1322' }} />
          <Statistic title="Margin" value={marginBps == null ? '—' : marginBps} precision={marginBps == null ? undefined : 1} suffix={marginBps == null ? undefined : 'bps'}
            valueStyle={{ fontSize: 18, color: marginBps == null ? undefined : marginBps < 0 ? '#cf1322' : marginBps < 5 ? '#d48806' : '#3f8600' }} />
          <Statistic title="自相关" value={selfCorr == null ? '—' : selfCorr} precision={selfCorr == null ? undefined : 2}
            valueStyle={{ fontSize: 18, color: selfCorr == null ? undefined : selfCorr > 0.7 ? '#cf1322' : selfCorr > 0.5 ? '#d48806' : '#3f8600' }} />
        </Space>

        <Divider style={{ margin: '12px 0' }} />

        {/* 4) 动作 */}
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <AntTooltip title={submitDisabledReason || '提交至 BRAIN（不可逆，消耗配额）'}>
            <Button type="primary" block icon={<CloudUploadOutlined />} disabled={submitDisabled} loading={submitLoading} onClick={onSubmit}>
              提交至 BRAIN
            </Button>
          </AntTooltip>
          <AntTooltip title="以该 alpha 为蓝本，对 decay/窗口/中性化 做设置扫描优化（消耗 BRAIN 配额）">
            <Button block icon={<ExperimentOutlined />} loading={optimizeLoading} onClick={onOpenOptimize}>
              以此为蓝本优化
            </Button>
          </AntTooltip>
        </Space>
      </Card>

      {/* 5) 人工反馈 */}
      <Card className="glass-card" style={{ marginTop: 12 }}
        title={<Space>人工反馈{alpha.human_feedback === 'NONE' && <Tag color="gold">需要你的评价</Tag>}</Space>}>
        <Space size="middle" wrap>
          <Button icon={<LikeOutlined />} type={alpha.human_feedback === 'LIKED' ? 'primary' : 'default'}
            loading={feedbackLoading} onClick={() => onFeedback('LIKED')}>👍 点赞</Button>
          <Button icon={<DislikeOutlined />} danger={alpha.human_feedback === 'DISLIKED'}
            loading={feedbackLoading} onClick={() => onFeedback('DISLIKED')}>👎 踩</Button>
        </Space>
        {alpha.feedback_comment && (
          <Text style={{ display: 'block', marginTop: 10 }} type="secondary">「{alpha.feedback_comment}」</Text>
        )}
      </Card>
    </div>
  )
}
```

- [ ] **Step 2: build 验证（组件可编译，尚未接入布局）**

Run: `cd "E:/WorldQuant/worldquant-alpha-aiac/frontend" && npm run build`
Expected: 成功（新组件未被引用也应通过，Vite 不报未用导出）。

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "refactor(alpha-detail): 新增 DecisionRail 决策栏组件"
```

---

## Task 6: 重排 index.jsx 布局为方案 B（Header + Hero + 双栏）

把容器 `index.jsx` 的 `return` 主体改成：Header → HeroMetrics → `Row`(左 DecisionRail / 右 工作区: 表达式 + 4 tab)。删除旧「提交决策卡」「双列表达式+元数据+危机」「底部反馈卡」结构（其内容已分别进入 DecisionRail / DetailsPanel / MarginalRiskPanel）。Modal 保留。

**Files:**
- Modify: `frontend/src/pages/AlphaDetail/index.jsx`

- [ ] **Step 1: 替换 return 主体**

保留文件上半部分所有 hooks/query/mutation/派生变量（`metrics`/`transitions`/`pnlData`/`analysis`/`alreadySubmitted`/`submitDisabled`/`submitDisabledReason`/`copyExpression` 等）不动；仅替换从 `return (` 到对应 `)` 的 JSX 为：

```jsx
import HeroMetrics from './HeroMetrics'
import DecisionRail from './DecisionRail'
import MarginalRiskPanel from './MarginalRiskPanel'
import PnlPanel from './PnlPanel'
import TransitionsPanel from './TransitionsPanel'
import DetailsPanel from './DetailsPanel'
// （顶部已有 Row/Col/Card/Tabs/Button/Tag/Space/Typography/Modal/Alert/InputNumber 等 antd 导入；
//   清理不再使用的导入：Descriptions/Timeline/Statistic/Divider/Empty/Spin 若仅旧布局用到可删，build 不强制。）

return (
  <div>
    {/* Header */}
    <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
      <Col>
        <Space wrap>
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/alphas')}>返回</Button>
          <Title level={3} style={{ margin: 0 }}>Alpha #{alpha.id}</Title>
          {alpha.alpha_id && (
            <AntTooltip title="点击复制 BRAIN Alpha ID">
              <Tag color="geekblue" style={{ cursor: 'pointer', fontFamily: 'monospace' }} onClick={copyBrainId} icon={<CopyOutlined />}>
                BRAIN: {alpha.alpha_id}
              </Tag>
            </AntTooltip>
          )}
          <Tag color={STATUS_COLORS[alpha.quality_status] || 'default'}>
            {STATUS_LABELS[alpha.quality_status] || alpha.quality_status}
          </Tag>
          {alpha.region && <Tag>{alpha.region} · {alpha.universe}</Tag>}
          {alreadySubmitted
            ? <AntTooltip title={`已提交至 BRAIN：${formatDateTime(alpha.date_submitted)}`}><Tag color="green">✅ 已提交</Tag></AntTooltip>
            : <Tag>⚪ 未提交</Tag>}
        </Space>
      </Col>
    </Row>

    {/* Hero 指标条（全宽） */}
    <HeroMetrics alpha={alpha} />

    {/* 双栏：决策栏 | 诊断工作区 */}
    <Row gutter={[16, 16]} align="top">
      <Col xs={24} lg={7} xl={6}>
        <DecisionRail
          alpha={alpha}
          metrics={metrics}
          analysis={analysis}
          marginalLoading={marginalLoading}
          marginalAutoPending={marginalEnabled && !marginal && !marginalError}
          onFetchMarginal={() => { setMarginalEnabled(true); if (marginalEnabled) refetchMarginal() }}
          refreshLoading={refreshCanSubmitMutation.isPending}
          onRefreshCanSubmit={() => refreshCanSubmitMutation.mutate()}
          submitDisabled={submitDisabled}
          submitDisabledReason={submitDisabledReason}
          submitLoading={submitMutation.isPending}
          onSubmit={() => submitMutation.mutate()}
          optimizeLoading={optimizeMutation.isPending}
          onOpenOptimize={() => setOptimizeOpen(true)}
          feedbackLoading={feedbackMutation.isPending}
          onFeedback={handleFeedback}
        />
      </Col>

      <Col xs={24} lg={17} xl={18}>
        {/* 表达式常驻 */}
        <Card className="glass-card" title="表达式"
          extra={<Button icon={<CopyOutlined />} size="small" onClick={copyExpression}>复制</Button>}>
          <pre style={{ fontSize: 14, lineHeight: 1.6, overflow: 'auto', maxHeight: 200, margin: 0 }}>{alpha.expression}</pre>
        </Card>

        {/* 诊断 tab */}
        <Card className="glass-card" style={{ marginTop: 16 }}>
          <Tabs
            defaultActiveKey="marginal"
            items={[
              {
                key: 'marginal',
                label: <Space><TrophyOutlined />边际贡献 &amp; 风险{alpha.can_submit && <Tag color="green">可提交</Tag>}</Space>,
                children: (
                  <MarginalRiskPanel
                    alpha={alpha}
                    marginal={marginal}
                    loading={marginalLoading}
                    error={marginalError}
                    enabled={marginalEnabled}
                    competition={marginalCompetition}
                    setCompetition={setMarginalCompetition}
                    onFetch={() => { setMarginalEnabled(true); if (marginalEnabled) refetchMarginal() }}
                    crisis={metrics?._crisis_correlations}
                  />
                ),
              },
              {
                key: 'pnl',
                label: <Space><LineChartOutlined />收益曲线{pnlData.length > 0 && <Tag>{pnlData.length}d</Tag>}</Space>,
                children: <PnlPanel pnlData={pnlData} loading={pnlLoading} />,
              },
              {
                key: 'details',
                label: <Space>详情</Space>,
                children: <DetailsPanel alpha={alpha} />,
              },
              {
                key: 'transitions',
                label: <Space><HistoryOutlined />状态变迁{transitions.length > 0 && <Tag>{transitions.length}</Tag>}</Space>,
                children: <TransitionsPanel transitions={transitions} loading={transLoading} />,
              },
            ]}
          />
        </Card>
      </Col>
    </Row>

    {/* 蓝本优化 Modal — 原样保留（原 807-845 行） */}
    {/* ... 原 Modal JSX 不动 ... */}
  </div>
)
```

- [ ] **Step 2: 清理无用导入与残留定义**

确认 `index.jsx` 不再引用 `Descriptions`/`Timeline`/`Empty`/`Spin`/`Divider`/`Statistic`/`LikeOutlined`/`DislikeOutlined`/`CloudUploadOutlined`/`ExperimentOutlined`/`ReloadOutlined`/`HistoryOutlined` 中哪些（被迁出的组件用到的删掉，容器仍用的留）。删除已迁出的 `CanSubmitTag`/`HeroMetrics`/`CrisisCorrelationPanel`/`MarginalPanel` 等任何残留定义。

> 注：Vite 不会因「导入未使用」报错，但保持整洁。判定标准仍是 build 通过。

- [ ] **Step 3: build 验证**

Run: `cd "E:/WorldQuant/worldquant-alpha-aiac/frontend" && npm run build`
Expected: 成功，无 "X is not defined" / "Could not resolve"。

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "refactor(alpha-detail): 重排为方案B布局(Header+Hero+决策栏|诊断工作区)"
```

---

## Task 7: 边际建议「智能条件拉」+ 决策栏吸顶微调

把 `marginalEnabled` 初值从恒 `false` 改为：当 `can_submit===true 且未提交 且有 alpha_id` 时自动开启拉取；其余手动。用 `useEffect` 在 `alpha` 到位后置位。

**Files:**
- Modify: `frontend/src/pages/AlphaDetail/index.jsx`

- [ ] **Step 1: 加 useEffect 智能触发**

在 `index.jsx` 顶部确保 `import { useState, useEffect } from 'react'`。在 `alpha` query 与 `marginalEnabled` state 声明之后加：

```jsx
// 智能条件拉：can_submit 通过且未提交的 alpha 进页自动拉边际建议；
// 其余情况(不可提交/已提交/无 BRAIN id)保持手动，避免无谓消耗 BRAIN 配额。
useEffect(() => {
  if (
    alpha &&
    alpha.can_submit === true &&
    !alpha.date_submitted &&
    alpha.alpha_id &&
    !marginalEnabled
  ) {
    setMarginalEnabled(true)
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
}, [alpha?.id, alpha?.can_submit, alpha?.date_submitted, alpha?.alpha_id])
```

> `useQuery` 的 `enabled: marginalEnabled && !!alpha?.alpha_id` 不变；一旦 `marginalEnabled` 变真即自动发起请求，决策栏 `marginalAutoPending` 显示「计算中」。

- [ ] **Step 2: build 验证**

Run: `cd "E:/WorldQuant/worldquant-alpha-aiac/frontend" && npm run build`
Expected: 成功。

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "feat(alpha-detail): 边际建议智能条件拉(可提交且未提交自动触发)"
```

---

## Task 8: 人工冒烟验证 + 收尾

无单测，故用真实页面冒烟覆盖验收标准。

**Files:** 无（仅验证）

- [ ] **Step 1: 起前端 dev（后端需在 8001 跑）**

Run: `cd "E:/WorldQuant/worldquant-alpha-aiac/frontend" && npm run dev`
打开 `http://localhost:5174/alphas/<某个 alpha id>`。

- [ ] **Step 2: 逐条对照验收标准**

- [ ] 宽屏向下滚动浏览 tab 时，左侧「提交决策 + 关键指标」**始终可见**（sticky 生效）。
- [ ] 打开一个 `can_submit=true 且未提交` 的 alpha → 决策栏边际块自动显示「计算中」并随后落地 SUBMIT/NEUTRAL/SKIP；打开一个已提交或不可提交的 alpha → 显示「获取边际建议」按钮，**不自动发请求**（Network 面板确认无 marginal 请求）。
- [ ] 「边际贡献 & 风险」tab 含前后对比 + 危机窗口相关性；「详情」tab 含假设/逻辑 + 元数据 + 字段 + 算子；收益曲线、状态变迁 tab 正常。
- [ ] 窄屏（拖窄到 < lg）退化为单列，顺序 Header→Hero→决策栏→表达式→tab；sticky 不再生效。
- [ ] 提交 / 优化 / 👍👎反馈 / 刷新校验 四个动作行为与改造前一致（含 disabled 原因 tooltip）。

- [ ] **Step 3: 终验 build**

Run: `cd "E:/WorldQuant/worldquant-alpha-aiac/frontend" && npm run build`
Expected: 成功。

- [ ] **Step 4: （如有微调）最终 commit**

```bash
git add -A && git commit -m "polish(alpha-detail): 冒烟修正"
```

---

## Self-Review

**Spec coverage（逐节核对）：**
- §2 方案 B 双栏 → Task 6 ✅
- §3.1 Header → Task 6 Step1 ✅
- §3.2 Hero 7 指标条 → Task 2 + Task 6 ✅
- §3.3 决策栏(can_submit/边际裁决/指标复述/提交·优化/反馈) → Task 5 ✅
- §3.4 工作区(表达式常驻 + 4 tab) → Task 4 + Task 6 ✅
- §3.4 合并决定(危机并入边际 / 假设·逻辑·元数据·字段·算子合并为详情) → Task 4 Step1/Step4 ✅
- §4 智能条件拉 → Task 7 ✅
- §5 响应式(lg 断点 + 窄屏取消 sticky) → Task 6(Col 断点) + Task 8 验证 ✅（sticky 仅 ≥lg 视觉生效；窄屏单列下 sticky 不致害）
- §6 组件拆分(9 文件) → Task 1-5 ✅
- §7 不变量(不改后端/语义/依赖) → 全程纯前端、API 调用未改 ✅
- §8 验收标准 → Task 8 逐条 ✅

**Placeholder 扫描：** 「原样粘贴(原 NNN 行)」是对**已存在于旧文件的代码**的精确搬迁指令（源码可见、行号明确），非待补内容；新增/改写的胶水代码(DecisionRail、新布局 return、useEffect、DetailsPanel)均给出完整代码。Modal 与若干 tab children 标「原样保留」并附行号，可直接定位。

**类型/命名一致性：** `pickMetric` 在 HeroMetrics.jsx 具名导出、DecisionRail 导入一致；`MarginalRiskPanel` 新增 `crisis` prop 在 Task4 定义、Task6 传入一致；`marginalAutoPending` 在 DecisionRail(Task5) 与 index.jsx(Task6) 命名一致；`onFetchMarginal`/`onFetch` 区分（决策栏手动入口 vs 面板内拉取按钮）均接同一 `setMarginalEnabled` 逻辑，一致。
