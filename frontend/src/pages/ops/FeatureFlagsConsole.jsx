import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Alert,
  Button,
  Card,
  Drawer,
  Empty,
  Popconfirm,
  Space,
  Spin,
  Switch,
  Table,
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

// Order the groups appear top→bottom in the page. Anything not in this
// list falls back to alphabetic order.
const GROUP_ORDER = ['P0', 'P1', 'P2-A', 'P2-B', 'P2-C', 'P2-D', 'P3-Brain']

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

  // Group the flat array into { groupName: [...flags] }, ordered by GROUP_ORDER
  const groupedFlags = useMemo(() => {
    const map = new Map()
    for (const f of flags) {
      const g = f.group || 'misc'
      if (!map.has(g)) map.set(g, [])
      map.get(g).push(f)
    }
    const ordered = []
    for (const g of GROUP_ORDER) {
      if (map.has(g)) {
        ordered.push([g, map.get(g)])
        map.delete(g)
      }
    }
    // Append any unknown groups alphabetically
    for (const g of [...map.keys()].sort()) {
      ordered.push([g, map.get(g)])
    }
    return ordered
  }, [flags])

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
      title: '开关名称',
      dataIndex: 'name',
      width: 320,
      render: (name, row) => (
        <Space>
          <Text strong style={{ fontFamily: 'monospace' }}>
            {name}
          </Text>
          <Tooltip title={row.description}>
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

      {/* P3-Brain — banner linking to dedicated /ops/brain-role page */}
      <BrainRoleEntryCard />

      {loading && flags.length === 0 ? (
        <div style={{ textAlign: 'center', padding: 40 }}>
          <Spin />
        </div>
      ) : groupedFlags.length === 0 ? (
        <Empty description="无可控开关（请检查后端白名单配置）" />
      ) : (
        groupedFlags.map(([group, rows]) => (
          <Card
            key={group}
            title={`分组 · ${group}`}
            size="small"
            style={{ marginTop: 16 }}
            className="glass-card"
          >
            <Table
              rowKey="name"
              size="small"
              columns={columns}
              dataSource={rows}
              pagination={false}
            />
          </Card>
        ))
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
