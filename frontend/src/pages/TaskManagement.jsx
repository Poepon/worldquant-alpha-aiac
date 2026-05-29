import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Row,
  Col,
  Card,
  Table,
  Button,
  Tag,
  Space,
  Typography,
  Modal,
  Drawer,
  Form,
  Input,
  Select,
  Switch,
  InputNumber,
  message,
  Alert,
  Tooltip as AntdTooltip,
} from 'antd'
import {
  PlusOutlined,
  ThunderboltOutlined,
  PlayCircleOutlined,
  PauseCircleOutlined,
  EyeOutlined,
  RocketOutlined,
} from '@ant-design/icons'
import api from '../services/api'
import { formatRelative } from '../utils/time'

const { Title } = Typography
const { Option } = Select
const { Search } = Input

// V-19 Persistent Mining Service: backend supports these regions
// phase15-D PR4b (2026-05-18): SESSION_REGIONS + SESSION_REGION_UNIVERSE
// constants removed alongside the cascade Card panel.


// Region to Universe mapping
const REGION_UNIVERSE_MAP = {
  USA: ['TOP3000', 'TOP1000', 'TOP500', 'TOP200', 'TOPSP500'],
  GLB: ['TOP3000', 'MINVOL1M', 'TOPDIV3000'],
  EUR: ['TOP1200'],
  ASI: ['MINVOL1M'],
  CHN: ['TOP2000U'],
  KOR: ['TOP600'],
  HKG: ['TOP500'],
  IND: ['TOP500'],
}

// Region names for display
const REGION_NAMES = {
  USA: 'USA (United States)',
  GLB: 'GLB (Global)',
  EUR: 'EUR (Europe)',
  ASI: 'ASI (Asia)',
  CHN: 'CHN (China)',
  KOR: 'KOR (South Korea)',
  HKG: 'HKG (Hong Kong)',
  IND: 'IND (India)',
}

export default function TaskManagement() {
  const [isModalOpen, setIsModalOpen] = useState(false)
  const [datasetStrategy, setDatasetStrategy] = useState('AUTO')
  const [selectedRegion, setSelectedRegion] = useState('USA')
  const [searchText, setSearchText] = useState('')
  const [isFlatDrawerOpen, setIsFlatDrawerOpen] = useState(false)
  const [flatRegion, setFlatRegion] = useState('USA')
  const [flatForm] = Form.useForm()

  // V-19: persistent mining service — primary single-button surface
  // phase15-D PR4b (2026-05-18): sessionRegion state + V-19 cascade
  // hooks removed alongside the cascade Card panel; flat sessions are
  // created via Ops Console.

  const [form] = Form.useForm()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  // phase15-D PR4b (2026-05-18): V-19 persistent mining session hooks
  // (useQuery miningSessions + 3 useMutations for start/stop/resume)
  // REMOVED — backend mining_session router gone (PR3c), api wrappers
  // dropped (this PR). Operators use POST /api/v1/ops/start-flat-session
  // via the Ops Console for new sessions.

  // Fetch tasks
  const { data: tasks, isLoading } = useQuery({
    queryKey: ['tasks'],
    queryFn: () => api.getTasks({ limit: 50 }),
    refetchInterval: 10000,
  })

  // 搜索过滤 — useMemo 避免每次 render 重算（任务规模上千时显著）
  const filteredTasks = useMemo(() => {
    const all = tasks || []
    if (!searchText) return all
    const q = searchText.toLowerCase()
    return all.filter((t) =>
      (t.task_name || '').toLowerCase().includes(q) ||
      (t.region || '').toLowerCase().includes(q) ||
      (t.universe || '').toLowerCase().includes(q) ||
      (t.schedule || '').toLowerCase().includes(q) ||
      (t.status || '').toLowerCase().includes(q),
    )
  }, [tasks, searchText])

  // Tier deep-link handler removed post tier-system removal (2026-05-18).
  // Stale ?mode=AUTONOMOUS_TIER... bookmarks are silently ignored — backend
  // TaskCreateRequest also accepts unknown fields with extra="ignore".

  // Create task mutation
  const createTaskMutation = useMutation({
    mutationFn: api.createTask,
    onSuccess: () => {
      message.success('任务创建成功')
      queryClient.invalidateQueries(['tasks'])
      setIsModalOpen(false)
      form.resetFields()
      setDatasetStrategy('AUTO')
      setSelectedRegion('USA')
    },
    onError: () => {
      message.error('任务创建失败')
    },
  })

  // Start task mutation
  const startTaskMutation = useMutation({
    mutationFn: api.startTask,
    onSuccess: () => {
      message.success('任务已启动')
      queryClient.invalidateQueries(['tasks'])
    },
  })

  // flat-F1 advanced kickoff — gated by ENABLE_FLAT_CONTINUOUS (backend 400)
  const startFlatSessionMutation = useMutation({
    mutationFn: api.startFlatSession,
    onSuccess: (info) => {
      message.success(`Flat Session 已启动 — task #${info.task_id} (${info.region}/${info.universe})`)
      queryClient.invalidateQueries(['tasks'])
      setIsFlatDrawerOpen(false)
      flatForm.resetFields()
    },
    onError: (err) => {
      const detail = err?.response?.data?.detail || err?.message || '未知错误'
      message.error(`启动失败：${detail}`)
    },
  })

  const handleStartFlatSession = (values) => {
    const datasetsList = (values.datasets || '')
      .split(/[\s,，\n]+/)
      .map((s) => s.trim())
      .filter(Boolean)
    startFlatSessionMutation.mutate({
      region: values.region,
      universe: values.universe,
      datasets: datasetsList,
      delay: values.delay ?? 1,
      enablePipeline: values.enablePipeline ?? false,
    })
  }

  const handleCreateTask = (values) => {
    // Format target_datasets as a list if it exists
    const payload = { ...values }
    if (payload.dataset_strategy === 'SPECIFIC' && payload.target_dataset_id) {
      payload.target_datasets = [payload.target_dataset_id]
      delete payload.target_dataset_id
    }
    createTaskMutation.mutate(payload)
  }

  // Handle region change to update universe options
  const handleRegionChange = (value) => {
    setSelectedRegion(value)
    // Default to strict top universe or first available
    const universes = REGION_UNIVERSE_MAP[value] || []
    form.setFieldsValue({ universe: universes[0] })
  }

  const columns = [
    {
      title: '任务名称',
      dataIndex: 'task_name',
      key: 'task_name',
      render: (text, record) => (
        <a onClick={() => navigate(`/tasks/${record.id}`)}>{text}</a>
      ),
    },
    {
      title: '地区',
      dataIndex: 'region',
      key: 'region',
      width: 100,
    },
    {
      title: '股票池',
      dataIndex: 'universe',
      key: 'universe',
      width: 120,
    },
    {
      title: '调度',
      dataIndex: 'schedule',
      key: 'schedule',
      width: 120,
      render: (schedule) => {
        const value = schedule || 'ONESHOT'
        const color = value === 'FLAT' ? 'purple' : 'blue'
        const label = value === 'FLAT' ? '持续挖掘' : '单次执行'
        return (
          <AntdTooltip title={`${value} — ${label}`}>
            <Tag color={color}>{label}</Tag>
          </AntdTooltip>
        )
      },
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 130,
      render: (status, record) => {
        const colors = {
          PENDING: 'default',
          RUNNING: 'processing',
          PAUSED: 'warning',
          COMPLETED: 'success',
          FAILED: 'error',
          STOPPED: 'default',
        }
        const reason = record?.config?.last_stop_reason
        // 醒目的退出原因(非自然完成)— 用户能区分"配额烧光"vs"BRAIN 断"vs"freeze"
        const reasonText = {
          max_iters_reached: '迭代上限',
          daily_goal_reached: '日 PASS 达标',
          completed: '正常完成',
          auth_circuit_open: 'BRAIN 认证断',
          ownership_lost: 'watchdog 移交',
          heartbeat_abort: 'freeze 兜底',
          task_paused: '外部 PAUSE',
          task_stopped: '外部 STOP',
          task_early_stopped: 'early stop',
          task_gone: 'task 不存在',
        }
        const reasonColor = ['auth_circuit_open', 'heartbeat_abort'].includes(reason)
          ? 'warning' : 'default'
        return (
          <Space direction="vertical" size={2}>
            <Tag color={colors[status] || 'default'}>{status}</Tag>
            {reason && status !== 'RUNNING' && (
              <Tag color={reasonColor} style={{ fontSize: 11 }}>
                {reasonText[reason] || reason}
              </Tag>
            )}
          </Space>
        )
      },
    },
    {
      title: '进度',
      key: 'progress',
      width: 100,
      render: (_, record) => (
        <span>{record.progress_current} / {record.daily_goal}</span>
      ),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 110,
      render: (date) => (
        <AntdTooltip title={date ? new Date(date).toLocaleString() : ''}>
          <span>{formatRelative(date)}</span>
        </AntdTooltip>
      ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 150,
      render: (_, record) => (
        <Space>
          {record.status === 'PENDING' && (
            <Button 
              size="small" 
              type="primary" 
              icon={<PlayCircleOutlined />}
              onClick={() => startTaskMutation.mutate(record.id)}
            >
              启动
            </Button>
          )}
          {record.status === 'RUNNING' && (
            <Button 
              size="small" 
              icon={<PauseCircleOutlined />}
              onClick={() => api.interveneTask(record.id, 'PAUSE')}
            >
              暂停
            </Button>
          )}
          <Button 
            size="small" 
            icon={<EyeOutlined />}
            onClick={() => navigate(`/tasks/${record.id}`)}
          >
            查看
          </Button>
        </Space>
      ),
    },
  ]

  // phase15-D PR4b (2026-05-18): primarySession/handlePrimary* helpers
  // removed alongside the cascade Card panel + api wrappers.

  return (
    <div>
      {/* Discrete task surface — cascade panel retired phase15-D PR3c.
          New persistent sessions go through Ops Console
          (/ops/start-flat-session). */}
      <Row justify="space-between" align="middle" style={{ marginBottom: 24 }}>
        <Col>
          <Title level={4} style={{ margin: 0 }}>
            <ThunderboltOutlined style={{ marginRight: 12, color: '#00d4ff' }} />
            离散任务（高级）
          </Title>
        </Col>
        <Col>
          <Space>
            <Button
              type="primary"
              icon={<RocketOutlined />}
              onClick={() => {
                setIsFlatDrawerOpen(true)
                flatForm.setFieldsValue({
                  region: 'USA',
                  universe: 'TOP3000',
                  datasets: '',
                })
                setFlatRegion('USA')
              }}
            >
              启动 Flat Session
            </Button>
            <Button
              icon={<PlusOutlined />}
              onClick={() => setIsModalOpen(true)}
            >
              创建离散任务
            </Button>
          </Space>
        </Col>
      </Row>

      <Card className="glass-card">
        <Search
          placeholder="按任务名 / 地区 / 模式 / 状态搜索..."
          allowClear
          value={searchText}
          onChange={(e) => setSearchText(e.target.value)}
          style={{ marginBottom: 12, maxWidth: 360 }}
        />
        <Table
          columns={columns}
          dataSource={filteredTasks}
          rowKey="id"
          loading={isLoading}
          pagination={{ pageSize: 20, showSizeChanger: true, showTotal: (t) => `共 ${t} 条` }}
          scroll={{ x: 1100 }}
        />
      </Card>

      {/* flat-F1 advanced kickoff Drawer — gated by ENABLE_FLAT_CONTINUOUS flag */}
      <Drawer
        title={
          <Space>
            <RocketOutlined />
            <span>启动 Flat Session</span>
          </Space>
        }
        width={480}
        open={isFlatDrawerOpen}
        onClose={() => setIsFlatDrawerOpen(false)}
        destroyOnHidden
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="Flat Continuous Mining"
          description={
            <span style={{ fontSize: 12 }}>
              假设驱动的扁平挖掘会话，不走 T1→T2→T3 级联。
              需先在 Ops Console 打开 <code>ENABLE_FLAT_CONTINUOUS</code> flag；
              flag OFF 时本表单会返回 400 并提示。
            </span>
          }
        />
        <Form
          form={flatForm}
          layout="vertical"
          onFinish={handleStartFlatSession}
          initialValues={{ region: 'USA', universe: 'TOP3000', datasets: '', delay: 1, enablePipeline: false }}
        >
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="region"
                label="地区"
                rules={[{ required: true }]}
              >
                <Select
                  onChange={(v) => {
                    setFlatRegion(v)
                    flatForm.setFieldsValue({ universe: (REGION_UNIVERSE_MAP[v] || [])[0] })
                  }}
                >
                  {Object.entries(REGION_NAMES).map(([key, name]) => (
                    <Option key={key} value={key}>{name}</Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="universe"
                label="股票池"
                rules={[{ required: true }]}
              >
                <Select>
                  {(REGION_UNIVERSE_MAP[flatRegion] || []).map((u) => (
                    <Option key={u} value={u}>{u}</Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
          </Row>
          <Form.Item
            name="datasets"
            label="数据集（可选）"
            tooltip="留空 = AUTO（自动选 dataset）；多个用逗号或换行分隔，例：analyst10, news4"
          >
            <Input.TextArea
              rows={3}
              placeholder="留空 = AUTO；或填 dataset_id，例如：analyst10, news4"
            />
          </Form.Item>
          <Form.Item
            name="delay"
            label="Delay"
            tooltip="delay-1 = 标准（用昨日数据，字段最全）；delay-0 = 当日数据（字段更稀疏、与 delay-1 正交的新挖掘面）。delay-0 需已同步该 universe 的 delay-0 字段。"
          >
            <Select>
              <Option value={1}>delay 1（标准）</Option>
              <Option value={0}>delay 0（当日 / 正交轴）</Option>
            </Select>
          </Form.Item>
          <Form.Item
            name="enablePipeline"
            label="挖掘流水线"
            valuePropName="checked"
            tooltip="开启 = 这个 session 走 producer-consumer 流水线（生成与 BRAIN 模拟重叠，保持 sim 槽饱和）。仅作用于本 session，不影响其它任务；默认关（串行 legacy 路径）。用于 shadow 验证。"
          >
            <Switch checkedChildren="流水线" unCheckedChildren="串行" />
          </Form.Item>
          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setIsFlatDrawerOpen(false)}>取消</Button>
              <Button
                type="primary"
                htmlType="submit"
                icon={<RocketOutlined />}
                loading={startFlatSessionMutation.isPending}
              >
                启动
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Drawer>

      {/* Create Task Modal */}
      <Modal
        title="创建挖掘任务"
        open={isModalOpen}
        onCancel={() => setIsModalOpen(false)}
        footer={null}
        width={600}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={handleCreateTask}
          initialValues={{
            region: 'USA',
            universe: 'TOP3000',
            dataset_strategy: 'AUTO',
            daily_goal: 4,
            max_iterations: 10,
            schedule: 'ONESHOT',
          }}
        >
          <Form.Item
            name="name"
            label="任务名称"
            rules={[{ required: true, message: '请输入任务名称' }]}
          >
            <Input placeholder="例如: 美股动量因子挖掘" />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="region" label="地区">
                <Select onChange={handleRegionChange}>
                  {Object.entries(REGION_NAMES).map(([key, name]) => (
                    <Option key={key} value={key}>{name}</Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="universe" label="股票池">
                <Select>
                  {(REGION_UNIVERSE_MAP[selectedRegion] || []).map(u => (
                    <Option key={u} value={u}>{u}</Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="dataset_strategy" label="数据集策略">
                <Select onChange={(val) => setDatasetStrategy(val)}>
                  <Option value="AUTO">自动探索（基于知识库 RAG）</Option>
                  <Option value="SPECIFIC">指定数据集</Option>
                </Select>
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="schedule"
                label="调度模式"
                tooltip="单次执行 = 跑一轮 DISCRETE 任务后结束;持续挖掘 = 持续 flat session(走运维控制台)"
                rules={[{ required: true, message: '请选择调度模式' }]}
              >
                <Select>
                  <Option value="ONESHOT">单次执行（ONESHOT）</Option>
                  <Option value="FLAT">持续挖掘（FLAT）</Option>
                </Select>
              </Form.Item>
            </Col>
          </Row>

          {datasetStrategy === 'SPECIFIC' && (
            <Form.Item
              name="target_dataset_id"
              label="数据集 ID"
              rules={[{ required: true, message: '请输入数据集 ID' }]}
              help="请输入 BRAIN 平台的数据集 ID (例如: analyst10, news4)"
            >
              <Input placeholder="输入 dataset_id" />
            </Form.Item>
          )}

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="daily_goal" label="每日目标 (Alpha 数量)">
                <InputNumber min={1} max={20} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="max_iterations"
                label="最大迭代次数"
                tooltip="≥5 让 typed Hypothesis 跨 round 累积 lifecycle 数据 (Plan v5+ Phase 2)"
              >
                <InputNumber min={1} max={100} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setIsModalOpen(false)}>取消</Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={createTaskMutation.isLoading}
              >
                创建
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
