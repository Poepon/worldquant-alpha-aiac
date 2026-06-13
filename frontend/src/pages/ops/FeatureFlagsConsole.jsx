import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Alert,
  Button,
  Card,
  Collapse,
  Drawer,
  Empty,
  Input,
  Popconfirm,
  Space,
  Spin,
  Switch,
  Table,
  Tabs,
  Tag,
  Timeline,
  Tooltip,
  Typography,
  message,
} from 'antd'
import {
  HistoryOutlined,
  InfoCircleOutlined,
  ReloadOutlined,
  SwapOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'

import api from '../../services/api'

const { Title, Text } = Typography

// Colors for the "source" badge — mirrors what the docs/ops_dashboard_guide
// will recommend so operators learn one color scheme across all /ops/* pages.
const SOURCE_TAG_COLORS = {
  env: 'default',
  default: 'default',
  'runtime-override': 'gold',
}

// 双轴控制台:顶部 3 个生命周期 tab × tab 内按功能域 (domain) 折叠分组。
const LIFECYCLE_TABS = [
  { key: 'operational', label: '🟢 运维' },
  { key: 'experimental', label: '🧪 实验' },
  { key: 'dormant', label: '💤 休眠' },
]
const DOMAIN_LABELS = {
  submit: '提交', rag: 'RAG', evaluation: '评估门', generation: '生成·prompt',
  'llm-routing': 'LLM 路由', regime: '市场体制', breadth: '广度·数据',
  brain: 'BRAIN 账号', kb: '知识库', misc: '其它',
}

// 这些 LLM 路由 flag 留在后端白名单(live 路由 + 配置中心编辑依赖),但不在本控制台展示;
// 它们经「配置中心 → LLM 厂商」管理。
const HIDDEN_IN_CONSOLE = new Set([
  'ENABLE_PER_FUNCTION_LLM_ROUTING',
  'LLM_FUNCTION_MODEL_MAP',
  'LLM_PROVIDERS',
  'LLM_AVAILABLE_MODELS',
])

// BrainRoleEntryCard — 轻量入口卡，跳转到 /ops/brain-role 专属页
function BrainRoleEntryCard() {
  const [state, setState] = useState(null)

  useEffect(() => {
    api.getBrainRoleState().then(setState).catch(() => setState(null))
  }, [])

  const isConsultant = state?.mode === 'CONSULTANT'
  const modeLabel = isConsultant ? '顾问模式' : '普通模式'
  return (
    <Alert
      type={isConsultant ? 'warning' : 'info'}
      showIcon
      icon={<SwapOutlined />}
      style={{ marginTop: 16 }}
      message={
        <Space>
          <span>BRAIN 账号模式</span>
          {state && <Tag color={isConsultant ? 'gold' : 'green'}>{modeLabel}</Tag>}
          <Link to="/ops/brain-role">前往 BRAIN 账号模式专属页 →</Link>
        </Space>
      }
      description={
        <Text type="secondary" style={{ fontSize: 12 }}>
          普通模式 ↔ 顾问模式 的切换、能力对比、操作确认都在专属页完成。
          <code>ENABLE_BRAIN_CONSULTANT_MODE</code> 这条开关在下方表里是只读的——
          直接切换会跳过批量回测控制位重置和全球数据同步任务。
        </Text>
      }
    />
  )
}

/**
 * Feature Flag Console (P3 — 2026-05-16)
 *
 * One Table per logical group (P0 / P1 / P2-*) so flags stay scannable as
 * the whitelist grows. Each row is a Switch that PATCHes immediately + a
 * Reset button that DELETEs the override. The Refresh-all button forces
 * the FastAPI process to re-read overrides from DB without waiting for
 * the 60s background refresher.
 *
 * Audit Drawer (right side) renders the most recent 50 flip/clear events
 * as an Ant Design Timeline. Opened from the "查看 audit" button.
 *
 * Errors from the backend (whitelist drift, type mismatch, 401/403)
 * surface as Ant message toasts and the Switch reverts to the prior
 * value — no optimistic update.
 */
export default function FeatureFlagsConsole() {
  const [flags, setFlags] = useState([])
  const [loading, setLoading] = useState(true)
  const [busyFlag, setBusyFlag] = useState(null)
  const [refreshingAll, setRefreshingAll] = useState(false)
  const [auditOpen, setAuditOpen] = useState(false)
  const [audit, setAudit] = useState([])
  const [auditLoading, setAuditLoading] = useState(false)
  const [activeTab, setActiveTab] = useState('operational')
  const [search, setSearch] = useState('')

  const fetchFlags = useCallback(async () => {
    setLoading(true)
    try {
      const data = await api.listFeatureFlags()
      setFlags(data)
    } catch (e) {
      message.error(`加载功能开关失败：${e?.response?.data?.detail || e.message}`)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchFlags()
  }, [fetchFlags])

  // 轴 1:按生命周期分桶(未知 lifecycle 落 dormant)
  const byLifecycle = useMemo(() => {
    const buckets = { operational: [], experimental: [], dormant: [] }
    for (const f of flags) {
      if (HIDDEN_IN_CONSOLE.has(f.name)) continue   // 隐藏 LLM 路由组(走配置中心)
      const lc = buckets[f.lifecycle] ? f.lifecycle : 'dormant'
      buckets[lc].push(f)
    }
    return buckets
  }, [flags])

  // 轴 2:当前 tab 内按功能域分组 + 搜索过滤(未知 domain 落 misc)
  const domainGroups = useMemo(() => {
    const q = search.trim().toLowerCase()
    const rows = byLifecycle[activeTab].filter(
      (f) => !q || (f.label || '').toLowerCase().includes(q) || f.name.toLowerCase().includes(q) || (f.description || '').toLowerCase().includes(q),
    )
    const map = new Map()
    for (const f of rows) {
      const d = DOMAIN_LABELS[f.domain] ? f.domain : 'misc'
      if (!map.has(d)) map.set(d, [])
      map.get(d).push(f)
    }
    return [...map.entries()].sort((a, b) => a[0].localeCompare(b[0]))
  }, [byLifecycle, activeTab, search])

  const handleToggle = async (flag, nextValue) => {
    setBusyFlag(flag.name)
    try {
      const updated = await api.setFeatureFlag(flag.name, nextValue)
      setFlags((prev) => prev.map((f) => (f.name === flag.name ? updated : f)))
      message.success(
        `${flag.name} → ${String(nextValue)}（60 秒内其他后台进程也会同步；点『全量刷新』可立即生效）`,
      )
    } catch (e) {
      message.error(
        `切换失败：${e?.response?.data?.detail || e.message}`,
      )
      // Refetch to re-sync UI with backend's authoritative state
      fetchFlags()
    } finally {
      setBusyFlag(null)
    }
  }

  const handleReset = async (flag) => {
    setBusyFlag(flag.name)
    try {
      const updated = await api.clearFeatureFlag(flag.name)
      setFlags((prev) => prev.map((f) => (f.name === flag.name ? updated : f)))
      message.success(`${flag.name} 覆盖已清除，回到环境变量默认值`)
    } catch (e) {
      message.error(`重置失败：${e?.response?.data?.detail || e.message}`)
      fetchFlags()
    } finally {
      setBusyFlag(null)
    }
  }

  const handleRefreshAll = async () => {
    setRefreshingAll(true)
    try {
      const { refreshed, flags: names } = await api.refreshAllFlags()
      message.success(
        `已强制刷新本进程缓存（共 ${refreshed} 条覆盖：${names.join('、') || '无'}）`,
      )
      await fetchFlags()
    } catch (e) {
      message.error(`全量刷新失败：${e?.response?.data?.detail || e.message}`)
    } finally {
      setRefreshingAll(false)
    }
  }

  const openAudit = async () => {
    setAuditOpen(true)
    setAuditLoading(true)
    try {
      const rows = await api.listFeatureFlagAudit(50)
      setAudit(rows)
    } catch (e) {
      message.error(`加载变更记录失败：${e?.response?.data?.detail || e.message}`)
      setAudit([])
    } finally {
      setAuditLoading(false)
    }
  }

  const columns = [
    {
      title: '开关',
      dataIndex: 'label',
      width: 320,
      render: (label, row) => (
        <Space>
          <Text strong>{label || row.name}</Text>
          <Tooltip
            title={
              <span>
                <code style={{ color: '#fff' }}>{row.name}</code>
                <br />
                {row.description}
              </span>
            }
          >
            <InfoCircleOutlined style={{ color: '#9c88ff' }} />
          </Tooltip>
        </Space>
      ),
    },
    {
      title: '类型',
      dataIndex: 'flag_type',
      width: 80,
      render: (t) => <Tag>{t}</Tag>,
    },
    {
      title: '当前值',
      width: 200,
      render: (_, row) => {
        // P3-Brain (2026-05-16): ENABLE_BRAIN_CONSULTANT_MODE 必须走顶部
        // OpsBrainRoleCard 的 Modal — 普通 Switch 直接 PATCH 会绕过 multi-sim
        // latch 清理 + sync_datasets enqueue,Consultant 模式有名无实。
        if (row.name === 'ENABLE_BRAIN_CONSULTANT_MODE') {
          return (
            <Tooltip title="此开关必须通过上方『BRAIN 账号模式』卡片切换 — 直接修改会跳过批量回测控制位重置和全球数据同步任务">
              <Tag color={row.effective_value ? 'gold' : 'default'}>
                {String(row.effective_value)} · 见上方卡片
              </Tag>
            </Tooltip>
          )
        }
        return row.flag_type === 'bool' ? (
          <Switch
            checked={!!row.effective_value}
            loading={busyFlag === row.name}
            onChange={(checked) => handleToggle(row, checked)}
          />
        ) : (
          // Non-bool types: read-only for now; PATCH still works through API
          // but Phase 1 only ships flip UX since every whitelisted flag is bool.
          <Text code>{String(row.effective_value)}</Text>
        )
      },
    },
    {
      title: '来源',
      dataIndex: 'source',
      width: 140,
      render: (src) => {
        const labels = {
          env: '环境变量',
          default: '默认值',
          'runtime-override': '运行时覆盖',
        }
        return <Tag color={SOURCE_TAG_COLORS[src] || 'default'}>{labels[src] || src}</Tag>
      },
    },
    {
      title: '默认值',
      dataIndex: 'env_default',
      width: 90,
      render: (v) => <Text code>{String(v)}</Text>,
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      width: 170,
      render: (v) => (v ? new Date(v).toLocaleString('zh-CN') : '—'),
    },
    {
      title: '操作人',
      dataIndex: 'updated_by',
      width: 120,
      render: (v) => v || '—',
    },
    {
      title: '操作',
      width: 90,
      render: (_, row) => {
        if (row.name === 'ENABLE_BRAIN_CONSULTANT_MODE') {
          return <Text type="secondary">↑ 见卡片</Text>
        }
        return row.source === 'runtime-override' ? (
          <Popconfirm
            title="清除此覆盖，回到环境变量默认值？"
            onConfirm={() => handleReset(row)}
          >
            <Button size="small" type="link">
              重置
            </Button>
          </Popconfirm>
        ) : (
          <Text type="secondary">—</Text>
        )
      },
    },
  ]

  return (
    <div>
      <Space
        style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}
        align="center"
      >
        <Title level={3} style={{ margin: 0 }}>
          功能开关控制台
        </Title>
        <Space>
          <Tooltip title="强制立即重读所有覆盖（不必等待 60 秒自动刷新）">
            <Button
              icon={<ThunderboltOutlined />}
              onClick={handleRefreshAll}
              loading={refreshingAll}
            >
              全量刷新
            </Button>
          </Tooltip>
          <Button icon={<HistoryOutlined />} onClick={openAudit}>
            查看变更记录
          </Button>
          <Button icon={<ReloadOutlined />} onClick={fetchFlags} loading={loading}>
            重新加载
          </Button>
        </Space>
      </Space>

      <Text type="secondary">
        切换开关后立即在当前进程生效；其他后台进程会在 60 秒内同步，点
        『全量刷新』可立即生效。『重置』= 清除运行时覆盖、回到环境变量默认值。
      </Text>
      <Text type="secondary" style={{ display: 'block', marginTop: 4, fontSize: 12 }}>
        LLM 模型路由 / 厂商 / 可选模型在「配置中心 → LLM 厂商」管理,不在此处。
      </Text>

      {/* P3-Brain — banner linking to dedicated /ops/brain-role page */}
      <BrainRoleEntryCard />

      {loading && flags.length === 0 ? (
        <div style={{ textAlign: 'center', padding: 40 }}>
          <Spin />
        </div>
      ) : flags.length === 0 ? (
        <Empty description="无可控开关（请检查后端白名单配置）" />
      ) : (
        <Card size="small" style={{ marginTop: 16 }} className="glass-card">
          <Input.Search
            placeholder="按中文名 / 开关名 / 描述过滤"
            allowClear
            style={{ maxWidth: 360, marginTop: 12 }}
            onChange={(e) => setSearch(e.target.value)}
          />
          <Tabs
            activeKey={activeTab}
            onChange={setActiveTab}
            items={LIFECYCLE_TABS.map((t) => ({
              key: t.key,
              label: `${t.label} (${byLifecycle[t.key].length})`,
            }))}
            style={{ marginTop: 8 }}
          />
          {domainGroups.length === 0 ? (
            <Empty description="该层无开关" />
          ) : (
            <Collapse
              defaultActiveKey={domainGroups.map(([d]) => d)}
              items={domainGroups.map(([domain, rows]) => ({
                key: domain,
                label: `${DOMAIN_LABELS[domain] || domain} · ${rows.length}`,
                children: (
                  <Table
                    rowKey="name"
                    size="small"
                    columns={columns}
                    dataSource={rows}
                    pagination={false}
                  />
                ),
              }))}
            />
          )}
        </Card>
      )}

      <Drawer
        title="功能开关变更记录（最近 50 条）"
        open={auditOpen}
        onClose={() => setAuditOpen(false)}
        width={520}
      >
        {auditLoading ? (
          <Spin />
        ) : audit.length === 0 ? (
          <Empty description="尚无变更记录" />
        ) : (
          <Timeline
            items={audit.map((a) => ({
              color: a.action === 'clear' ? 'gray' : 'blue',
              children: (
                <div>
                  <Text strong style={{ fontFamily: 'monospace' }}>
                    {a.flag_name}
                  </Text>
                  <Tag style={{ marginLeft: 8 }}>
                    {a.action === 'set' ? '设置' : a.action === 'clear' ? '清除' : a.action}
                  </Tag>
                  <div style={{ marginTop: 4, fontSize: 12, color: '#888' }}>
                    {new Date(a.created_at).toLocaleString('zh-CN')} · {a.actor}
                  </div>
                  <div style={{ marginTop: 4 }}>
                    <Text type="secondary">原值：</Text>{' '}
                    <Text code>{a.old_value ?? 'null'}</Text>{' '}
                    <Text type="secondary">→ 新值：</Text>{' '}
                    <Text code>{a.new_value}</Text>
                  </div>
                  {a.note && (
                    <div style={{ marginTop: 4, fontStyle: 'italic', color: '#aaa' }}>
                      {a.note}
                    </div>
                  )}
                </div>
              ),
            }))}
          />
        )}
      </Drawer>
    </div>
  )
}
