import { useState, useEffect, useMemo } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
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
  Form,
  Input,
  Select,
  InputNumber,
  message,
  Alert,
  Descriptions,
} from 'antd'
import {
  PlusOutlined,
  ThunderboltOutlined,
  PlayCircleOutlined,
  PauseCircleOutlined,
  EyeOutlined,
} from '@ant-design/icons'
import api from '../services/api'

const { Title } = Typography
const { Option } = Select


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

// PR3: tier-aware preset thresholds shown alongside the agent_mode selector
const TIER_PREVIEW = {
  AUTONOMOUS_TIER1:
    'sharpe ≥ 0.8 · fitness ≥ 0.5 · turnover [0.01, 0.70] · sub-universe ≥ 0.1 · 不查 self_corr / concentrated',
  AUTONOMOUS_TIER2:
    'sharpe ≥ 1.0 · fitness ≥ 0.8 · turnover [0.01, 0.55] · sub-universe ≥ 0.2 · 检查 concentrated · 不查 self_corr',
  AUTONOMOUS_TIER3:
    'sharpe ≥ 1.5 · fitness ≥ 1.0 · turnover [0.01, 0.70] · sub-universe ≥ BRAIN 动态 · self_corr verified < 0.7',
  AUTONOMOUS:
    'sharpe ≥ 1.5 · fitness ≥ 1.0 · turnover ≤ 0.70 · self_corr < 0.7 (legacy thresholds)',
}


export default function TaskManagement() {
  const [isModalOpen, setIsModalOpen] = useState(false)
  const [datasetStrategy, setDatasetStrategy] = useState('AUTO')
  const [selectedRegion, setSelectedRegion] = useState('USA')
  const [agentMode, setAgentMode] = useState('AUTONOMOUS')

  const [form] = Form.useForm()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  // Fetch tasks
  const { data: tasks, isLoading } = useQuery({
    queryKey: ['tasks'],
    queryFn: () => api.getTasks({ limit: 50 }),
    refetchInterval: 10000,
  })

  // PR3: handle ?mode=AUTONOMOUS_TIER2&seed_alpha_id=123 deep-link from
  // FactorLibrary's "派生 →" button. Opens the create modal with mode and
  // task_name pre-filled.
  useEffect(() => {
    const modeFromUrl = searchParams.get('mode')
    const seedId = searchParams.get('seed_alpha_id')
    if (modeFromUrl) {
      setIsModalOpen(true)
      setAgentMode(modeFromUrl)
      const initial = {
        agent_mode: modeFromUrl,
        region: 'USA',
        universe: 'TOP3000',
        dataset_strategy: 'AUTO',
        daily_goal: 4,
        max_iterations: 10,
        name: seedId
          ? `${modeFromUrl.replace('AUTONOMOUS_', '')} from #${seedId}`
          : '',
      }
      form.setFieldsValue(initial)
      // Clear the URL params after consuming so refresh doesn't reopen
      setSearchParams({})
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // PR3: seed availability check for T2/T3 modes (drives start-button enable)
  const tierFromMode = useMemo(() => {
    if (agentMode === 'AUTONOMOUS_TIER2') return 2
    if (agentMode === 'AUTONOMOUS_TIER3') return 3
    return null
  }, [agentMode])

  const { data: seedAvail } = useQuery({
    queryKey: ['seed-availability', tierFromMode, selectedRegion],
    queryFn: () => api.getSeedAvailability(tierFromMode, selectedRegion),
    enabled: !!tierFromMode && !!selectedRegion,
  })

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
      title: '模式',
      dataIndex: 'agent_mode',
      key: 'agent_mode',
      width: 120,
      render: (mode) => (
        <Tag color={mode === 'AUTONOMOUS' ? 'blue' : 'purple'}>
          {mode}
        </Tag>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (status) => {
        const colors = {
          PENDING: 'default',
          RUNNING: 'processing',
          PAUSED: 'warning',
          COMPLETED: 'success',
          FAILED: 'error',
          STOPPED: 'default',
        }
        return <Tag color={colors[status] || 'default'}>{status}</Tag>
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
      width: 180,
      render: (date) => new Date(date).toLocaleString(),
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

  return (
    <div>
      <Row justify="space-between" align="middle" style={{ marginBottom: 24 }}>
        <Col>
          <Title level={3} style={{ margin: 0 }}>
            <ThunderboltOutlined style={{ marginRight: 12, color: '#00d4ff' }} />
            任务管理
          </Title>
        </Col>
        <Col>
          <Button 
            type="primary" 
            icon={<PlusOutlined />}
            onClick={() => setIsModalOpen(true)}
          >
            创建任务
          </Button>
        </Col>
      </Row>

      <Card className="glass-card">
        <Table
          columns={columns}
          dataSource={tasks || []}
          rowKey="id"
          loading={isLoading}
          pagination={{ pageSize: 10 }}
        />
      </Card>

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
            agent_mode: 'AUTONOMOUS',
            daily_goal: 4,
            max_iterations: 10,
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
                  <Option value="AUTO">自动探索 (Hierarchical RAG)</Option>
                  <Option value="SPECIFIC">指定数据集</Option>
                </Select>
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="agent_mode" label="Agent 模式">
                <Select onChange={(v) => setAgentMode(v)}>
                  <Option value="AUTONOMOUS">自动 (Legacy)</Option>
                  <Option value="AUTONOMOUS_TIER1">T1 — 一阶（裸 ts_op）</Option>
                  <Option value="AUTONOMOUS_TIER2">T2 — 二阶（横截面 / 平滑包装）</Option>
                  <Option value="AUTONOMOUS_TIER3">T3 — 三阶（trade_when 择时）</Option>
                  <Option value="INTERACTIVE">交互 (Step-by-step)</Option>
                </Select>
              </Form.Item>
            </Col>
          </Row>

          {/* PR3: tier preview banner — shows the PASS thresholds for the
              currently selected mode so users have realistic expectations */}
          {TIER_PREVIEW[agentMode] && (
            <Alert
              type={agentMode.startsWith('AUTONOMOUS_TIER') ? 'info' : 'warning'}
              message={`PASS 阈值预览（${agentMode}）`}
              description={TIER_PREVIEW[agentMode]}
              style={{ marginBottom: 16 }}
              showIcon
            />
          )}

          {/* PR3: seed availability for T2/T3 — fetched from
              /factor-library/seed-availability for the chosen region */}
          {tierFromMode && seedAvail && (
            <Alert
              type={seedAvail.is_ready ? 'success' : 'warning'}
              message={
                seedAvail.is_ready
                  ? `T${tierFromMode} 种子可用：${seedAvail.available_seeds} 条 PASS T${
                      tierFromMode - 1
                    } alpha 在 ${seedAvail.region}（最少需 ${seedAvail.min_required}）`
                  : `T${tierFromMode} 种子不足：${seedAvail.available_seeds}/${seedAvail.min_required}`
              }
              description={
                seedAvail.is_ready
                  ? null
                  : `请先跑 AUTONOMOUS_TIER${
                      tierFromMode - 1
                    } 任务积累 ≥${seedAvail.min_required} 条 PASS 种子，再启动本任务`
              }
              style={{ marginBottom: 16 }}
              showIcon
            />
          )}

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
                disabled={tierFromMode && seedAvail && !seedAvail.is_ready}
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
