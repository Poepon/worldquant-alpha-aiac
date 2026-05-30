import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Alert,
  Button,
  Card,
  Drawer,
  Empty,
  Input,
  Popconfirm,
  Select,
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
  DeleteOutlined,
  HistoryOutlined,
  InfoCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  SaveOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'

import api from '../../services/api'

const { Title, Text, Paragraph } = Typography

const FLAG_ENABLE = 'ENABLE_PER_FUNCTION_LLM_ROUTING'
const FLAG_MAP = 'LLM_FUNCTION_MODEL_MAP'
const FLAG_MODELS = 'LLM_AVAILABLE_MODELS'

// Provider whitelist — the OpenAI-compatible llm_service only knows these two.
const PROVIDERS = ['openai', 'anthropic']

// Fallback model list if LLM_AVAILABLE_MODELS flag is unreadable / empty.
// Mirrors backend config._load_llm_available_models() defaults.
const FALLBACK_MODELS = [
  'deepseek-v4-pro',
  'deepseek-v4-flash',
  'kimi-k2.6',
  'kimi-k2.5',
  'qwen3.7-max',
  'qwen3.6-plus',
  'qwen3.6-flash',
  'glm-5.1',
  'glm-5',
]

// Typical node_keys shown as quick-add suggestions when the map is empty —
// mirrors backend config defaults. Not authoritative; operators can add any key.
const SUGGESTED_NODE_KEYS = [
  'hypothesis',
  'code_gen',
  'self_correct',
  'r1b_retry',
  'llm_mutate_alpha',
  'llm_crossover_alpha',
  'r1b_mutate',
  'r5_alignment_c1',
  'r5_alignment_c2',
  'attribution',
]

let _rowSeq = 0
const nextRowId = () => {
  _rowSeq += 1
  return `__row_${_rowSeq}`
}

// Coerce a feature-flag effective_value (which the backend returns as a decoded
// json object/array, or null) into the row array the table renders.
function mapToRows(mapObj) {
  if (!mapObj || typeof mapObj !== 'object' || Array.isArray(mapObj)) return []
  return Object.entries(mapObj).map(([node_key, entry]) => {
    const e = entry && typeof entry === 'object' ? entry : {}
    return {
      _id: nextRowId(),
      node_key,
      model: e.model || '',
      provider: e.provider || 'openai',
      base_url: e.base_url || '',
      thinking_effort: e.thinking_effort || '',
    }
  })
}

// Assemble the table rows back into the json the backend expects. Optional
// fields (base_url / thinking_effort) only included when non-empty.
function rowsToMap(rows) {
  const out = {}
  for (const r of rows) {
    const key = (r.node_key || '').trim()
    if (!key) continue
    const entry = { model: (r.model || '').trim(), provider: r.provider || 'openai' }
    if (r.base_url && r.base_url.trim()) entry.base_url = r.base_url.trim()
    if (r.thinking_effort && r.thinking_effort.trim()) {
      entry.thinking_effort = r.thinking_effort.trim()
    }
    out[key] = entry
  }
  return out
}

/**
 * LLM Routing Console (PR4 — 2026-05-29)
 *
 * Edits the per-function LLM routing config that backend resolve_model_for
 * reads from the LLM_FUNCTION_MODEL_MAP json feature-flag. Three flags back
 * this page (all group="LLM-Routing"):
 *   - ENABLE_PER_FUNCTION_LLM_ROUTING (bool master switch, top toggle)
 *   - LLM_FUNCTION_MODEL_MAP (json node_key→{model,provider,...}, main table)
 *   - LLM_AVAILABLE_MODELS (json string[], feeds the model dropdown)
 *
 * The master switch PATCHes immediately (with refetch-on-failure rollback —
 * no optimistic update). The mapping table is staged locally and committed
 * as one json PATCH via the 保存映射 button, after client-side schema
 * validation (every entry needs a non-empty model; provider ∈ {openai,
 * anthropic}; unknown models warn but are allowed). 立即生效 forces the
 * FastAPI process to re-read overrides (refresh-all) so changes apply without
 * waiting for the 60s background refresher. The audit Drawer reuses the
 * shared flag-audit timeline.
 */
export default function LLMRoutingConsole() {
  const [loading, setLoading] = useState(true)
  const [savingMap, setSavingMap] = useState(false)
  const [togglingMaster, setTogglingMaster] = useState(false)
  const [refreshingAll, setRefreshingAll] = useState(false)

  const [masterOn, setMasterOn] = useState(false)
  const [masterSource, setMasterSource] = useState('default')
  const [availableModels, setAvailableModels] = useState(FALLBACK_MODELS)
  const [rows, setRows] = useState([])

  const [auditOpen, setAuditOpen] = useState(false)
  const [audit, setAudit] = useState([])
  const [auditLoading, setAuditLoading] = useState(false)

  const fetchAll = useCallback(async () => {
    setLoading(true)
    try {
      const flags = await api.listFeatureFlags()
      const byName = {}
      for (const f of flags) byName[f.name] = f

      const enableFlag = byName[FLAG_ENABLE]
      setMasterOn(!!enableFlag?.effective_value)
      setMasterSource(enableFlag?.source || 'default')

      const modelsFlag = byName[FLAG_MODELS]
      const modelsVal = modelsFlag?.effective_value
      setAvailableModels(
        Array.isArray(modelsVal) && modelsVal.length > 0
          ? modelsVal.map(String)
          : FALLBACK_MODELS,
      )

      const mapFlag = byName[FLAG_MAP]
      setRows(mapToRows(mapFlag?.effective_value))
    } catch (e) {
      message.error(`加载路由配置失败：${e?.response?.data?.detail || e.message}`)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchAll()
  }, [fetchAll])

  // Model dropdown options = available list ∪ any model already in use (so a
  // pre-existing custom model still shows its current value instead of blank).
  const modelOptions = useMemo(() => {
    const set = new Set(availableModels)
    for (const r of rows) if (r.model) set.add(r.model)
    return [...set].map((m) => ({ label: m, value: m }))
  }, [availableModels, rows])

  const updateRow = (id, patch) => {
    setRows((prev) => prev.map((r) => (r._id === id ? { ...r, ...patch } : r)))
  }

  const removeRow = (id) => {
    setRows((prev) => prev.filter((r) => r._id !== id))
  }

  const addRow = () => {
    setRows((prev) => [
      ...prev,
      {
        _id: nextRowId(),
        node_key: '',
        model: availableModels[0] || '',
        provider: 'openai',
        base_url: '',
        thinking_effort: '',
      },
    ])
  }

  const handleToggleMaster = async (checked) => {
    setTogglingMaster(true)
    try {
      const updated = await api.setFeatureFlag(FLAG_ENABLE, checked)
      setMasterOn(!!updated?.effective_value)
      setMasterSource(updated?.source || 'runtime-override')
      message.success(
        `${FLAG_ENABLE} → ${String(checked)}（点『立即生效』可让其它后台进程立刻同步）`,
      )
    } catch (e) {
      message.error(`切换失败：${e?.response?.data?.detail || e.message}`)
      fetchAll() // re-sync with authoritative backend state (rollback)
    } finally {
      setTogglingMaster(false)
    }
  }

  // Client-side schema validation before committing the json PATCH.
  // Returns { ok, errors[], warnings[] }.
  const validateRows = useCallback(() => {
    const errors = []
    const warnings = []
    const seenKeys = new Set()
    const known = new Set(availableModels)

    rows.forEach((r, idx) => {
      const label = (r.node_key || '').trim() || `第 ${idx + 1} 行`
      const key = (r.node_key || '').trim()
      if (!key) {
        errors.push(`${label}：node_key 不能为空`)
      } else if (seenKeys.has(key)) {
        errors.push(`node_key 重复：${key}`)
      } else {
        seenKeys.add(key)
      }
      if (!r.model || !r.model.trim()) {
        errors.push(`${label}：model 不能为空`)
      } else if (!known.has(r.model.trim())) {
        warnings.push(`${label}：model「${r.model.trim()}」不在可选清单内（允许，但请确认拼写）`)
      }
      if (!PROVIDERS.includes(r.provider)) {
        errors.push(`${label}：provider 必须是 ${PROVIDERS.join(' / ')}`)
      }
    })
    return { ok: errors.length === 0, errors, warnings }
  }, [rows, availableModels])

  const handleSaveMap = async () => {
    const { ok, errors, warnings } = validateRows()
    if (!ok) {
      message.error({
        content: (
          <div>
            <div>映射校验未通过，已阻止保存：</div>
            {errors.map((e, i) => (
              <div key={i}>· {e}</div>
            ))}
          </div>
        ),
        duration: 6,
      })
      return
    }
    if (warnings.length > 0) {
      warnings.forEach((w) => message.warning(w, 5))
    }
    setSavingMap(true)
    try {
      const payload = rowsToMap(rows)
      const updated = await api.setFeatureFlag(FLAG_MAP, payload)
      // Re-hydrate from the authoritative stored value.
      setRows(mapToRows(updated?.effective_value ?? payload))
      message.success(
        `路由映射已保存（${Object.keys(payload).length} 个 node_key）。点『立即生效』让本 API 进程立刻读取；跑挖掘的 worker 进程走自身 60s 后台刷新（≤60s 生效）。`,
      )
    } catch (e) {
      message.error(`保存失败：${e?.response?.data?.detail || e.message}`)
      fetchAll()
    } finally {
      setSavingMap(false)
    }
  }

  const handleRefreshAll = async () => {
    setRefreshingAll(true)
    try {
      const { refreshed, flags: names } = await api.refreshAllFlags()
      message.success(
        `已强制刷新本 API 进程缓存（共 ${refreshed} 条覆盖：${(names || []).join('、') || '无'}）。worker 进程不受此影响，走自身 60s 后台刷新。`,
      )
      await fetchAll()
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
      const all = await api.listFeatureFlagAudit(100)
      // Only show events for the three LLM-Routing flags.
      const relevant = (all || []).filter((a) =>
        [FLAG_ENABLE, FLAG_MAP, FLAG_MODELS].includes(a.flag_name),
      )
      setAudit(relevant)
    } catch (e) {
      message.error(`加载变更记录失败：${e?.response?.data?.detail || e.message}`)
      setAudit([])
    } finally {
      setAuditLoading(false)
    }
  }

  const columns = [
    {
      title: 'node_key',
      dataIndex: 'node_key',
      width: 200,
      render: (val, row) => (
        <Input
          value={val}
          placeholder="如 hypothesis / code_gen"
          onChange={(e) => updateRow(row._id, { node_key: e.target.value })}
          style={{ fontFamily: 'monospace' }}
        />
      ),
    },
    {
      title: '当前 model',
      dataIndex: 'model',
      width: 160,
      render: (val) => <Text code>{val || '—'}</Text>,
    },
    {
      title: '选择 model',
      width: 220,
      render: (_, row) => (
        <Select
          value={row.model || undefined}
          placeholder="选择模型"
          showSearch
          allowClear
          style={{ width: '100%' }}
          options={modelOptions}
          onChange={(v) => updateRow(row._id, { model: v || '' })}
          // Allow free-typed model not in the list (warn-but-allow in validation)
          onSearch={() => {}}
          filterOption={(input, opt) =>
            (opt?.label ?? '').toLowerCase().includes(input.toLowerCase())
          }
        />
      ),
    },
    {
      title: 'provider',
      dataIndex: 'provider',
      width: 140,
      render: (val, row) => (
        <Select
          value={val}
          style={{ width: '100%' }}
          options={PROVIDERS.map((p) => ({ label: p, value: p }))}
          onChange={(v) => updateRow(row._id, { provider: v })}
        />
      ),
    },
    {
      title: 'base_url (可选)',
      dataIndex: 'base_url',
      width: 200,
      render: (val, row) => (
        <Input
          value={val}
          placeholder="覆盖默认 base_url"
          onChange={(e) => updateRow(row._id, { base_url: e.target.value })}
        />
      ),
    },
    {
      title: 'thinking_effort (可选)',
      dataIndex: 'thinking_effort',
      width: 160,
      render: (val, row) => (
        <Input
          value={val}
          placeholder="如 high / low"
          onChange={(e) => updateRow(row._id, { thinking_effort: e.target.value })}
        />
      ),
    },
    {
      title: '操作',
      width: 70,
      render: (_, row) => (
        <Popconfirm title="删除此 node_key 路由？" onConfirm={() => removeRow(row._id)}>
          <Button size="small" type="link" danger icon={<DeleteOutlined />} />
        </Popconfirm>
      ),
    },
  ]

  return (
    <div>
      <Space
        style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}
        align="center"
      >
        <Title level={3} style={{ margin: 0 }}>
          LLM 按功能路由控制台
        </Title>
        <Space>
          <Tooltip title="强制立即重读所有覆盖（不必等待 60 秒自动刷新）">
            <Button
              icon={<ThunderboltOutlined />}
              onClick={handleRefreshAll}
              loading={refreshingAll}
            >
              立即生效
            </Button>
          </Tooltip>
          <Button icon={<HistoryOutlined />} onClick={openAudit}>
            查看变更记录
          </Button>
          <Button icon={<ReloadOutlined />} onClick={fetchAll} loading={loading}>
            重新加载
          </Button>
        </Space>
      </Space>

      <Paragraph type="secondary">
        为每个功能块（node_key）路由到不同 LLM 模型。热路径（hypothesis / code_gen）走质量优模型，
        辅助路径（self_correct / retry）走便宜快模型。修改映射后点『保存映射』提交，再点『立即生效』让
        本进程与其它后台进程立刻读取（否则 60 秒内自动同步）。底层三个 Flag 都在{' '}
        <Link to="/ops/feature-flags">Feature Flag 控制台</Link> 的{' '}
        <Text code>LLM-Routing</Text> 分组中。
      </Paragraph>

      <Card size="small" className="glass-card" style={{ marginBottom: 16 }}>
        <Space size="large" align="center">
          <Space>
            <Text strong>总开关</Text>
            <Tooltip title="OFF=所有 node 走全局默认模型（byte-for-byte legacy）。ON=按下表为每个 node 选模型。">
              <InfoCircleOutlined style={{ color: '#9c88ff' }} />
            </Tooltip>
          </Space>
          <Switch
            checked={masterOn}
            loading={togglingMaster}
            checkedChildren="开"
            unCheckedChildren="关"
            onChange={handleToggleMaster}
          />
          <Text code style={{ fontFamily: 'monospace' }}>
            {FLAG_ENABLE}
          </Text>
          <Tag color={masterSource === 'runtime-override' ? 'gold' : 'default'}>
            {masterSource === 'runtime-override'
              ? '运行时覆盖'
              : masterSource === 'env'
                ? '环境变量'
                : '默认值'}
          </Tag>
          {!masterOn && (
            <Text type="secondary">（当前关闭 — 下表配置不生效，所有 node 走全局默认模型）</Text>
          )}
        </Space>
      </Card>

      <Card
        title="功能块 → 模型 映射"
        size="small"
        className="glass-card"
        extra={
          <Space>
            <Button icon={<PlusOutlined />} onClick={addRow}>
              新增 node_key
            </Button>
            <Button
              type="primary"
              icon={<SaveOutlined />}
              loading={savingMap}
              onClick={handleSaveMap}
            >
              保存映射
            </Button>
          </Space>
        }
      >
        {loading && rows.length === 0 ? (
          <div style={{ textAlign: 'center', padding: 40 }}>
            <Spin />
          </div>
        ) : rows.length === 0 ? (
          <Empty
            description={
              <Space direction="vertical" align="center">
                <span>暂无路由映射。点『新增 node_key』开始配置。</span>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  典型 node_key：{SUGGESTED_NODE_KEYS.join(' / ')}
                </Text>
              </Space>
            }
          />
        ) : (
          <Table
            rowKey="_id"
            size="small"
            columns={columns}
            dataSource={rows}
            pagination={false}
            scroll={{ x: 1150 }}
          />
        )}
      </Card>

      <Alert
        type="info"
        showIcon
        style={{ marginTop: 16 }}
        message="可选模型清单来自 LLM_AVAILABLE_MODELS Flag"
        description={
          <Space wrap>
            {availableModels.map((m) => (
              <Tag key={m}>{m}</Tag>
            ))}
          </Space>
        }
      />

      <Drawer
        title="LLM-Routing Flag 变更记录"
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
                    {a.created_at ? new Date(a.created_at).toLocaleString('zh-CN') : ''} ·{' '}
                    {a.actor}
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
