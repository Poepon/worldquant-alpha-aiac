import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Drawer,
  Empty,
  Modal,
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

const { Title, Text, Paragraph } = Typography

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

/**
 * OpsBrainRoleCard (P3-Brain — 2026-05-16)
 *
 * Manual switch for BRAIN Consultant mode. Backend at /ops/brain/* — only
 * activates after user confirms they received BRAIN upgrade email.
 * Direction-C semantics (see plan §14):
 *   - Data-consistency capabilities (Sharpe / testPeriod): frozen per task snapshot
 *   - Endpoint-selection capabilities (multi-sim / PROD-corr): global flag, immediate
 * So switching mid-run is safe for in-flight tasks' Sharpe/testPeriod but
 * immediately stops them from calling Consultant-only endpoints.
 */
function OpsBrainRoleCard() {
  const [state, setState] = useState(null)
  const [loading, setLoading] = useState(true)
  const [switching, setSwitching] = useState(false)
  const [modalOpen, setModalOpen] = useState(false)
  const [acknowledged, setAcknowledged] = useState(false)

  const fetchState = useCallback(async () => {
    setLoading(true)
    try {
      const data = await api.getBrainRoleState()
      setState(data)
    } catch (e) {
      message.error(`加载 BRAIN role state 失败:${e?.response?.data?.detail || e.message}`)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchState()
  }, [fetchState])

  const isConsultant = state?.mode === 'CONSULTANT'

  const openSwitchModal = () => {
    setAcknowledged(false)
    setModalOpen(true)
  }

  const handleConfirmSwitch = async () => {
    setSwitching(true)
    try {
      const fn = isConsultant ? api.deactivateConsultant : api.activateConsultant
      const result = await fn()
      message.success(
        isConsultant
          ? '已切回 USER 模式(running task 的 Sharpe/testPeriod 不变;multi-sim/PROD-corr 立即降级)'
          : `已切到 CONSULTANT 模式${result?.sync_enqueued ? '(后台全球数据同步已触发)' : ''}`,
      )
      setModalOpen(false)
      await fetchState()
    } catch (e) {
      message.error(`切换失败:${e?.response?.data?.detail || e.message}`)
    } finally {
      setSwitching(false)
    }
  }

  return (
    <Card
      size="small"
      style={{ marginTop: 16 }}
      title={
        <Space>
          <SwapOutlined />
          BRAIN 模式
          {state && (
            <Tag color={isConsultant ? 'gold' : 'green'}>
              {state.mode}
            </Tag>
          )}
        </Space>
      }
      extra={
        <Space>
          <Button onClick={fetchState} loading={loading} size="small">
            刷新
          </Button>
          {state && (
            <Button
              type={isConsultant ? 'default' : 'primary'}
              onClick={openSwitchModal}
              size="small"
            >
              {isConsultant ? '切回 USER 模式' : '切换到 CONSULTANT 模式'}
            </Button>
          )}
        </Space>
      }
    >
      {loading && !state ? (
        <Spin />
      ) : state ? (
        <>
          <Descriptions column={2} size="small" bordered>
            <Descriptions.Item label="上次切换">
              {state.last_switched_at
                ? `${new Date(state.last_switched_at).toLocaleString()} (by ${state.last_switched_by || '—'})`
                : '从未'}
            </Descriptions.Item>
            <Descriptions.Item label="正在运行 task">
              {state.running_tasks_count} 个(快照已冻结,不受切换影响)
            </Descriptions.Item>
            <Descriptions.Item label="effective Sharpe 提交门槛">
              {state.effective_sharpe_submit_min}
            </Descriptions.Item>
            <Descriptions.Item label="effective testPeriod">
              {state.effective_default_test_period}
            </Descriptions.Item>
            <Descriptions.Item label="effective regions" span={2}>
              <Space wrap>
                {Object.entries(state.effective_region_universes).map(([r, u]) => (
                  <Tag key={r}>{`${r}/${u}`}</Tag>
                ))}
              </Space>
            </Descriptions.Item>
          </Descriptions>
        </>
      ) : (
        <Empty description="无 state 数据" />
      )}

      <Modal
        title={isConsultant ? '切回 USER 模式' : '切换到 CONSULTANT 模式'}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={handleConfirmSwitch}
        okButtonProps={{ disabled: !acknowledged, loading: switching }}
        okText={isConsultant ? '确认切回 USER' : '确认切换到 CONSULTANT'}
        cancelText="取消"
        width={640}
      >
        {!isConsultant ? (
          <>
            <Alert
              type="warning"
              showIcon
              message="请确认你已收到 BRAIN 平台 Consultant 升级邮件"
              style={{ marginBottom: 12 }}
            />
            <Paragraph>切换到 CONSULTANT 模式后:</Paragraph>
            <ul>
              <li>立即触发后台 5 region 同步(USA/CHN/HKG/JPN/EUR,预计 10-30 分钟)</li>
              <li>新发起 task 使用 testPeriod=P0Y、Sharpe 提交门槛抬到 1.58</li>
              <li>当前 {state?.running_tasks_count ?? 0} 个 running task <b>不受影响</b>(读启动时冻结的配置)</li>
              <li>
                <Text type="warning">
                  <b>注意 legacy alpha</b>:task_id=NULL(v5 之前创建)的旧 alpha 在下次 sync 时会被用 Sharpe=1.58 重判 → 可能批量
                  PASS → PASS_PROVISIONAL 降级。建议切换前先回填 task_id,或接受此一次性降级。
                </Text>
              </li>
              <li><b>安全网</b>:若 BRAIN 在下次 submit_alpha 时返回 PROD-corr 403(账号实际未授权),系统会<b>自动切回 USER</b> 并写 audit 日志</li>
            </ul>
          </>
        ) : (
          <>
            <Alert
              type="info"
              showIcon
              message="切回 USER 模式 — 应用层立即停止调用 Consultant-only BRAIN endpoint"
              style={{ marginBottom: 12 }}
            />
            <Paragraph>切回 USER 模式后:</Paragraph>
            <ul>
              <li>已在跑的 Consultant task 的 <b>Sharpe 门槛、testPeriod 设置</b> 仍按启动时配置(数据一致性保留)</li>
              <li><b>Multi-simulation 立即降级为 single-sim 循环</b>(吞吐率下降 ~10-30x;若 task 还有大量 alpha 待 sim,evaluate 时间会显著拉长)</li>
              <li><b>PROD-correlation 第 3 门 gate 立即停跑</b>;该 task 后续提交的 alpha 只过 self_corr precheck — BRAIN 服务端可能在 submit 时拒</li>
              <li>如需完全停止 task,在任务详情页用 "intervene" 操作收尾(/api/v1/tasks/&#123;id&#125;/intervene → status=COMPLETED)</li>
            </ul>
          </>
        )}
        <Checkbox
          checked={acknowledged}
          onChange={(e) => setAcknowledged(e.target.checked)}
        >
          我已阅读并理解上述变化
        </Checkbox>
      </Modal>
    </Card>
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
      message.error(`加载 flag 失败:${e?.response?.data?.detail || e.message}`)
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
        `${flag.name} → ${String(nextValue)}(60s 内 worker 进程也会感知;点 "全量刷新" 立即生效)`,
      )
    } catch (e) {
      message.error(
        `flip 失败:${e?.response?.data?.detail || e.message}`,
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
      message.success(`${flag.name} override 已清除,回落 env 默认`)
    } catch (e) {
      message.error(`reset 失败:${e?.response?.data?.detail || e.message}`)
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
        `已强制刷新本进程缓存(共 ${refreshed} 条 override:${names.join(', ') || '无'})`,
      )
      await fetchFlags()
    } catch (e) {
      message.error(`refresh-all 失败:${e?.response?.data?.detail || e.message}`)
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
      message.error(`加载 audit 失败:${e?.response?.data?.detail || e.message}`)
      setAudit([])
    } finally {
      setAuditLoading(false)
    }
  }

  const columns = [
    {
      title: 'Flag',
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
            <Tooltip title="此 flag 必须在上方 'BRAIN 模式' 卡片切换 — 直接 PATCH 会绕过 multi-sim latch 清理与全球数据同步">
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
      render: (src) => (
        <Tag color={SOURCE_TAG_COLORS[src] || 'default'}>{src}</Tag>
      ),
    },
    {
      title: 'env 默认',
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
            title="清除此 override,回落 env 默认?"
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
          Feature Flag 控制台
        </Title>
        <Space>
          <Tooltip title="强制本进程立即从 DB 重读所有 override(无需等 60s 刷新)">
            <Button
              icon={<ThunderboltOutlined />}
              onClick={handleRefreshAll}
              loading={refreshingAll}
            >
              全量刷新
            </Button>
          </Tooltip>
          <Button icon={<HistoryOutlined />} onClick={openAudit}>
            查看 audit
          </Button>
          <Button icon={<ReloadOutlined />} onClick={fetchFlags} loading={loading}>
            重新加载
          </Button>
        </Space>
      </Space>

      <Text type="secondary">
        翻转开关后写入 DB + 当前进程立即生效;其他 worker 进程在 60s 内同步,或点
        "全量刷新" 强制本进程刷新。重置 = 清除 override 回落到 env 默认。
      </Text>

      {/* P3-Brain — BRAIN Consultant mode 手动切换卡片(在 flag table 之前) */}
      <OpsBrainRoleCard />

      {loading && flags.length === 0 ? (
        <div style={{ textAlign: 'center', padding: 40 }}>
          <Spin />
        </div>
      ) : groupedFlags.length === 0 ? (
        <Empty description="无可控 flag(检查 SUPPORTED_FLAGS 白名单)" />
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
        title="Feature Flag 审计日志(最近 50 条)"
        open={auditOpen}
        onClose={() => setAuditOpen(false)}
        width={520}
      >
        {auditLoading ? (
          <Spin />
        ) : audit.length === 0 ? (
          <Empty description="尚无 audit 记录" />
        ) : (
          <Timeline
            items={audit.map((a) => ({
              color: a.action === 'clear' ? 'gray' : 'blue',
              children: (
                <div>
                  <Text strong style={{ fontFamily: 'monospace' }}>
                    {a.flag_name}
                  </Text>
                  <Tag style={{ marginLeft: 8 }}>{a.action}</Tag>
                  <div style={{ marginTop: 4, fontSize: 12, color: '#888' }}>
                    {new Date(a.created_at).toLocaleString('zh-CN')} · {a.actor}
                  </div>
                  <div style={{ marginTop: 4 }}>
                    <Text type="secondary">old:</Text>{' '}
                    <Text code>{a.old_value ?? 'null'}</Text>{' '}
                    <Text type="secondary">→ new:</Text>{' '}
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
