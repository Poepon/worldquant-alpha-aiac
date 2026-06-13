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

// ---------------------------------------------------------------------------
// Hero metric strip — headline IS metrics at the top of the page.
// returns/drawdown are ratios (×100 → %), margin is a ratio (×10000 → bps),
// self_corr / sharpe / fitness / turnover are shown raw.
// ---------------------------------------------------------------------------
export default function HeroMetrics({ alpha }) {
  const sharpe = pickMetric(alpha, 'sharpe')
  const fitness = pickMetric(alpha, 'fitness')
  const returns = pickMetric(alpha, 'returns')
  const turnover = pickMetric(alpha, 'turnover')
  const drawdown = pickMetric(alpha, 'drawdown')
  const margin = pickMetric(alpha, 'margin')
  const m = alpha.metrics || {}
  const selfCorr =
    typeof m._self_corr === 'number'
      ? m._self_corr
      : typeof m.selfCorrelation === 'number'
        ? m.selfCorrelation
        : null

  const tiles = [
    {
      key: 'sharpe', title: 'Sharpe', tip: '年化超额收益 / 年化波动率',
      value: sharpe, precision: 2,
      color: sharpe == null ? undefined : sharpe >= 1.5 ? '#3f8600' : sharpe >= 1.0 ? '#d48806' : '#cf1322',
    },
    {
      key: 'fitness', title: 'Fitness', tip: 'BRAIN 综合评分 (Sharpe × √收益 / √换手率)',
      value: fitness, precision: 2,
      color: fitness == null ? undefined : fitness >= 1.0 ? '#3f8600' : '#d48806',
    },
    {
      key: 'returns', title: '年化收益', tip: '年化收益率',
      value: returns == null ? null : returns * 100, precision: 1, suffix: '%',
    },
    {
      key: 'turnover', title: '换手率', tip: '日均持仓变化比例 (越低交易成本越小)',
      value: turnover, precision: 2,
    },
    {
      key: 'drawdown', title: '最大回撤', tip: '回测期内最大回撤',
      value: drawdown == null ? null : drawdown * 100, precision: 1, suffix: '%',
      color: drawdown == null ? undefined : '#cf1322',
    },
    {
      key: 'margin', title: 'Margin', tip: '每单位交易利润；约 5bps 为盈亏成本线',
      value: margin == null ? null : margin * 10000, precision: 1, suffix: ' bps',
      color: margin == null ? undefined : margin < 0 ? '#cf1322' : margin * 10000 < 5 ? '#d48806' : '#3f8600',
    },
    {
      key: 'self_corr', title: '自相关', tip: '与已提交 alpha 的最高相关度；> 0.7 不可提交',
      value: selfCorr, precision: 2,
      color: selfCorr == null ? undefined : selfCorr > 0.7 ? '#cf1322' : selfCorr > 0.5 ? '#d48806' : '#3f8600',
    },
  ]

  return (
    <Card className="glass-card" style={{ marginBottom: 16 }}>
      <Row gutter={[16, 16]}>
        {tiles.map((t) => (
          <Col key={t.key} xs={12} sm={8} md={6} lg={3} style={{ minWidth: 104 }}>
            <Statistic
              title={<AntTooltip title={t.tip}><span>{t.title}</span></AntTooltip>}
              value={t.value == null ? '—' : t.value}
              precision={t.value == null ? undefined : t.precision}
              suffix={t.value == null ? undefined : t.suffix}
              valueStyle={{ color: t.color, fontSize: 20 }}
            />
          </Col>
        ))}
      </Row>
    </Card>
  )
}
