import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Row,
  Col,
  Card,
  Typography,
  Tabs,
  Slider,
  Switch,
  Table,
  Tag,
  Button,
  Space,
  InputNumber,
  Form,
  Input,
  message,
  Alert,
  Spin,
  Tooltip,
  Divider,
  Select,
  Modal,
  Descriptions,
  Popconfirm,
} from 'antd'
import {
  SettingOutlined,
  SaveOutlined,
  KeyOutlined,
  CloudOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  SyncOutlined,
  EyeInvisibleOutlined,
  EyeTwoTone,
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  ClusterOutlined,
} from '@ant-design/icons'
import api from '../services/api'

const { Title, Text, Paragraph } = Typography

export default function ConfigCenter() {
  const [brainForm] = Form.useForm()

  // Fetch knowledge entries
  const { data: successPatterns, isLoading: patternsLoading } = useQuery({
    queryKey: ['knowledge', 'success-patterns'],
    queryFn: () => api.getSuccessPatterns(30),
  })

  const { data: failurePitfalls, isLoading: pitfallsLoading } = useQuery({
    queryKey: ['knowledge', 'failure-pitfalls'],
    queryFn: () => api.getFailurePitfalls(30),
  })

  // Fetch credentials status
  const { data: credentialsData, isLoading: credentialsLoading, refetch: refetchCredentials } = useQuery({
    queryKey: ['credentials'],
    queryFn: api.getCredentialsStatus,
  })

  // Mutations for credentials
  const saveBrainCredentialsMutation = useMutation({
    mutationFn: ({ email, password }) => api.setBrainCredentials(email, password),
    onSuccess: () => {
      message.success('Brain 平台凭证保存成功')
      refetchCredentials()
      brainForm.resetFields()
    },
    onError: (error) => {
      message.error(`保存失败: ${error.response?.data?.detail || error.message}`)
    },
  })

  const testBrainCredentialsMutation = useMutation({
    mutationFn: api.testBrainCredentials,
    onSuccess: () => {
      message.success('Brain 平台连接测试成功！')
    },
    onError: (error) => {
      message.error(`连接测试失败: ${error.response?.data?.detail || error.message}`)
    },
  })

  const knowledgeColumns = [
    {
      title: '模式',
      dataIndex: 'pattern',
      key: 'pattern',
      width: 200,
    },
    {
      title: '描述',
      dataIndex: 'description',
      key: 'description',
      ellipsis: true,
    },
    {
      title: '使用次数',
      dataIndex: 'usage_count',
      key: 'usage_count',
      width: 80,
    },
    {
      title: '状态',
      dataIndex: 'is_active',
      key: 'is_active',
      width: 80,
      render: (active) => (
        <Tag color={active ? 'success' : 'default'}>
          {active ? '启用' : '禁用'}
        </Tag>
      ),
    },
    {
      title: '来源',
      dataIndex: 'created_by',
      key: 'created_by',
      width: 90,
      render: (source) => {
        const label = source === 'USER' ? '用户' : source === 'SYSTEM' ? '系统' : source
        return <Tag color={source === 'USER' ? 'blue' : 'default'}>{label}</Tag>
      },
    },
  ]

  // Credentials tab content
  const CredentialsTab = () => {
    const credentials = credentialsData?.credentials || {}

    const renderCredentialStatus = (key, label) => {
      const cred = credentials[key] || {}
      const isSet = cred.is_set
      const source = cred.source
      
      return (
        <div style={{ 
          display: 'flex', 
          justifyContent: 'space-between', 
          alignItems: 'center',
          padding: '8px 0',
          borderBottom: '1px solid rgba(255,255,255,0.1)'
        }}>
          <Text>{label}</Text>
          <Space>
            {isSet ? (
              <>
                <Text type="secondary" style={{ fontFamily: 'monospace' }}>
                  {cred.masked}
                </Text>
                {source === 'env' && (
                  <Tooltip title="从环境变量读取">
                    <Tag color="blue">ENV</Tag>
                  </Tooltip>
                )}
                <CheckCircleOutlined style={{ color: '#52c41a' }} />
              </>
            ) : (
              <>
                <Text type="secondary">(未配置)</Text>
                <CloseCircleOutlined style={{ color: '#ff4d4f' }} />
              </>
            )}
          </Space>
        </div>
      )
    }

    return (
      <Row gutter={24}>
        {/* Brain Platform Credentials */}
        <Col xs={24} lg={12}>
          <Card 
            className="glass-card" 
            title={
              <Space>
                <CloudOutlined style={{ color: '#00d4ff' }} />
                <span>WorldQuant Brain 平台</span>
              </Space>
            }
          >
            <Alert
              message="Brain 平台凭证"
              description="用于连接 WorldQuant Brain 平台进行 Alpha 模拟和数据同步。"
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
            />

            {credentialsLoading ? (
              <Spin />
            ) : (
              <div style={{ marginBottom: 24 }}>
                <Title level={5}>当前状态</Title>
                {renderCredentialStatus('brain_email', '邮箱')}
                {renderCredentialStatus('brain_password', '密码')}
              </div>
            )}

            <Divider />

            <Title level={5}>更新凭证</Title>
            <Form
              form={brainForm}
              layout="vertical"
              onFinish={(values) => {
                saveBrainCredentialsMutation.mutate(values)
              }}
            >
              <Form.Item
                name="email"
                label="Brain 平台邮箱"
                rules={[
                  { required: true, message: '请输入邮箱' },
                  { type: 'email', message: '请输入有效的邮箱地址' }
                ]}
              >
                <Input 
                  prefix={<KeyOutlined />} 
                  placeholder="your-email@example.com" 
                />
              </Form.Item>

              <Form.Item
                name="password"
                label="Brain 平台密码"
                rules={[{ required: true, message: '请输入密码' }]}
              >
                <Input.Password 
                  prefix={<KeyOutlined />}
                  placeholder="输入密码"
                  iconRender={(visible) => (visible ? <EyeTwoTone /> : <EyeInvisibleOutlined />)}
                />
              </Form.Item>

              <Form.Item>
                <Space>
                  <Button 
                    type="primary" 
                    htmlType="submit"
                    icon={<SaveOutlined />}
                    loading={saveBrainCredentialsMutation.isPending}
                  >
                    保存凭证
                  </Button>
                  <Button 
                    icon={<SyncOutlined />}
                    onClick={() => testBrainCredentialsMutation.mutate()}
                    loading={testBrainCredentialsMutation.isPending}
                  >
                    测试连接
                  </Button>
                </Space>
              </Form.Item>
            </Form>
          </Card>
        </Col>
      </Row>
    )
  }

  // LLM Providers tab — named endpoint+key registry consumed by the ops
  // LLM-Routing console (routing entries reference a provider by name).
  const LLMProvidersTab = () => {
    const queryClient = useQueryClient()
    const [providerForm] = Form.useForm()
    const [modalOpen, setModalOpen] = useState(false)
    const [editing, setEditing] = useState(null) // null=add, else provider name

    const { data: providers = [], isLoading } = useQuery({
      queryKey: ['llm-providers'],
      queryFn: api.listLLMProviders,
    })

    const saveMutation = useMutation({
      mutationFn: api.saveLLMProvider,
      onSuccess: (res) => {
        message.success(res?.message || '厂商已保存')
        queryClient.invalidateQueries({ queryKey: ['llm-providers'] })
        setModalOpen(false)
        providerForm.resetFields()
        setEditing(null)
      },
      onError: (error) => {
        message.error(`保存失败: ${error.response?.data?.detail || error.message}`)
      },
    })

    const deleteMutation = useMutation({
      mutationFn: api.deleteLLMProvider,
      onSuccess: (res) => {
        message.success(res?.message || '厂商已删除')
        queryClient.invalidateQueries({ queryKey: ['llm-providers'] })
      },
      onError: (error) => {
        message.error(`删除失败: ${error.response?.data?.detail || error.message}`)
      },
    })

    const openAdd = () => {
      setEditing(null)
      providerForm.resetFields()
      providerForm.setFieldsValue({ sdk: 'openai' })
      setModalOpen(true)
    }

    const openEdit = (row) => {
      setEditing(row.name)
      providerForm.setFieldsValue({
        name: row.name,
        label: row.label,
        sdk: row.sdk,
        base_url: row.base_url,
        api_key: '', // blank = keep existing secret
      })
      setModalOpen(true)
    }

    const handleSubmit = () => {
      providerForm.validateFields().then((vals) => {
        saveMutation.mutate({
          name: vals.name,
          label: vals.label || vals.name,
          sdk: vals.sdk,
          baseUrl: vals.base_url || '',
          apiKey: vals.api_key || '',
        })
      })
    }

    const columns = [
      {
        title: '厂商标识',
        dataIndex: 'name',
        width: 160,
        render: (v) => <Text code style={{ fontFamily: 'monospace' }}>{v}</Text>,
      },
      { title: '展示名', dataIndex: 'label', width: 160 },
      {
        title: '接口类型',
        dataIndex: 'sdk',
        width: 110,
        render: (v) => <Tag color={v === 'anthropic' ? 'purple' : 'geekblue'}>{v}</Tag>,
      },
      {
        title: '接口地址',
        dataIndex: 'base_url',
        ellipsis: true,
        render: (v) => v
          ? <Text type="secondary" style={{ fontFamily: 'monospace', fontSize: 12 }}>{v}</Text>
          : <Text type="secondary">（SDK 默认）</Text>,
      },
      {
        title: '密钥',
        dataIndex: 'has_key',
        width: 100,
        render: (has) => has
          ? <Tag color="success" icon={<CheckCircleOutlined />}>已配置</Tag>
          : <Tag color="error" icon={<CloseCircleOutlined />}>缺失</Tag>,
      },
      {
        title: '操作',
        width: 130,
        render: (_, row) => (
          <Space>
            <Button size="small" type="link" icon={<EditOutlined />} onClick={() => openEdit(row)}>
              编辑
            </Button>
            <Popconfirm
              title={`删除厂商「${row.name}」？`}
              description="模型路由表中引用此厂商的条目将回退到全局默认。"
              onConfirm={() => deleteMutation.mutate(row.name)}
            >
              <Button size="small" type="link" danger icon={<DeleteOutlined />} />
            </Popconfirm>
          </Space>
        ),
      },
    ]

    return (
      <Card
        className="glass-card"
        title={
          <Space>
            <ClusterOutlined />
            <span>LLM 厂商注册表</span>
          </Space>
        }
        extra={
          <Button type="primary" icon={<PlusOutlined />} onClick={openAdd}>
            新增厂商
          </Button>
        }
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="预先配置各模型厂商的接口地址 + 密钥，模型路由表按名称引用"
          description={
            <span>
              在这里登记不同 LLM 厂商（接口地址 + 密钥，密钥加密存库）。随后到{' '}
              <Text code>运维 → LLM 模型路由控制台</Text> 为每个功能模块选择厂商并填模型。
              密钥永不回显；编辑时留空表示保留原密钥。Claude 原生（anthropic）接口的接口地址留空即用官方默认。
            </span>
          }
        />
        <Table
          rowKey="name"
          size="small"
          loading={isLoading}
          columns={columns}
          dataSource={providers}
          pagination={false}
          locale={{ emptyText: '暂无厂商，点『新增厂商』开始配置' }}
        />

        <Modal
          title={editing ? `编辑厂商：${editing}` : '新增 LLM 厂商'}
          open={modalOpen}
          onOk={handleSubmit}
          confirmLoading={saveMutation.isPending}
          onCancel={() => { setModalOpen(false); setEditing(null); providerForm.resetFields() }}
          okText="保存"
          cancelText="取消"
        >
          <Form form={providerForm} layout="vertical" requiredMark>
            <Form.Item
              name="name"
              label="厂商标识"
              tooltip="唯一标识，仅字母/数字/下划线/连字符，如 moonshot、aliyun_maas"
              rules={[
                { required: true, message: '请输入厂商标识' },
                { pattern: /^[A-Za-z0-9_-]+$/, message: '仅允许字母/数字/下划线/连字符' },
              ]}
            >
              <Input placeholder="moonshot" disabled={!!editing} style={{ fontFamily: 'monospace' }} />
            </Form.Item>
            <Form.Item name="label" label="展示名">
              <Input placeholder="Moonshot 官方" />
            </Form.Item>
            <Form.Item name="sdk" label="接口类型" rules={[{ required: true }]}>
              <Select
                options={[
                  { value: 'openai', label: 'openai（OpenAI 兼容：DeepSeek/Qwen/Kimi/GLM/Moonshot…）' },
                  { value: 'anthropic', label: 'anthropic（Claude 原生）' },
                ]}
              />
            </Form.Item>
            <Form.Item
              name="base_url"
              label="接口地址 (API Base URL)"
              tooltip="Claude 原生（anthropic）接口留空=官方 api.anthropic.com；OpenAI 兼容厂商必填"
            >
              <Input placeholder="https://api.moonshot.cn/v1" />
            </Form.Item>
            <Form.Item
              name="api_key"
              label="API 密钥"
              tooltip="加密存储，永不回显。编辑时留空=保留原密钥"
              rules={editing ? [] : [{ required: true, message: '新增厂商必须填写密钥' }]}
            >
              <Input.Password placeholder={editing ? '留空保留原密钥' : 'sk-...'} autoComplete="new-password" />
            </Form.Item>
          </Form>
        </Modal>
      </Card>
    )
  }

  const tabs = [
    {
      key: 'credentials',
      label: (
        <Space>
          <KeyOutlined />
          凭证管理
        </Space>
      ),
      children: <CredentialsTab />,
    },
    {
      key: 'llm-providers',
      label: (
        <Space>
          <ClusterOutlined />
          LLM 厂商
        </Space>
      ),
      children: <LLMProvidersTab />,
    },
    {
      key: 'thresholds',
      label: '质量阈值',
      children: (
        <Card className="glass-card">
          <Form layout="vertical" style={{ maxWidth: 500 }}>
            <Form.Item label="最低夏普比率 (Sharpe Ratio)">
              <Row gutter={16}>
                <Col span={16}>
                  <Slider 
                    min={0} 
                    max={5} 
                    step={0.1} 
                    defaultValue={1.5}
                    marks={{ 0: '0', 1: '1', 1.5: '1.5', 2: '2', 3: '3', 5: '5' }}
                  />
                </Col>
                <Col span={8}>
                  <InputNumber min={0} max={5} step={0.1} defaultValue={1.5} style={{ width: '100%' }} />
                </Col>
              </Row>
            </Form.Item>

            <Form.Item label="最高换手率 (Turnover)">
              <Row gutter={16}>
                <Col span={16}>
                  <Slider 
                    min={0} 
                    max={2} 
                    step={0.1} 
                    defaultValue={0.7}
                    marks={{ 0: '0', 0.5: '0.5', 1: '1', 1.5: '1.5', 2: '2' }}
                  />
                </Col>
                <Col span={8}>
                  <InputNumber min={0} max={2} step={0.1} defaultValue={0.7} style={{ width: '100%' }} />
                </Col>
              </Row>
            </Form.Item>

            <Form.Item label="最低适应度 (Fitness)">
              <Row gutter={16}>
                <Col span={16}>
                  <Slider 
                    min={0} 
                    max={1} 
                    step={0.05} 
                    defaultValue={0.6}
                    marks={{ 0: '0', 0.5: '0.5', 1: '1' }}
                  />
                </Col>
                <Col span={8}>
                  <InputNumber min={0} max={1} step={0.05} defaultValue={0.6} style={{ width: '100%' }} />
                </Col>
              </Row>
            </Form.Item>

            <Form.Item label="最大相关度（多样性 / 与已有策略的不重复程度）">
              <Row gutter={16}>
                <Col span={16}>
                  <Slider 
                    min={0} 
                    max={1} 
                    step={0.05} 
                    defaultValue={0.7}
                    marks={{ 0: '0', 0.5: '0.5', 0.7: '0.7', 1: '1' }}
                  />
                </Col>
                <Col span={8}>
                  <InputNumber min={0} max={1} step={0.05} defaultValue={0.7} style={{ width: '100%' }} />
                </Col>
              </Row>
            </Form.Item>

            <Form.Item>
              <Button type="primary" icon={<SaveOutlined />}>
                保存设置
              </Button>
            </Form.Item>
          </Form>
        </Card>
      ),
    },
    {
      key: 'operators',
      label: '算子偏好',
      children: (
        <Card className="glass-card">
          <Table
            dataSource={[
              { operator: 'ts_rank', usage: 234, success_rate: 78, status: 'ACTIVE' },
              { operator: 'ts_corr', usage: 189, success_rate: 82, status: 'ACTIVE' },
              { operator: 'ts_zscore', usage: 156, success_rate: 75, status: 'ACTIVE' },
              { operator: 'grouped_rank', usage: 98, success_rate: 71, status: 'ACTIVE' },
              { operator: 'ts_product', usage: 45, success_rate: 12, status: 'BANNED' },
            ]}
            columns={[
              { title: '算子', dataIndex: 'operator', key: 'operator' },
              { title: '使用次数', dataIndex: 'usage', key: 'usage' },
              { 
                title: '成功率', 
                dataIndex: 'success_rate', 
                key: 'success_rate',
                render: (rate) => (
                  <Text style={{ color: rate > 50 ? '#00ff88' : '#ff4757' }}>
                    {rate}%
                  </Text>
                ),
              },
              {
                title: '状态',
                dataIndex: 'status',
                key: 'status',
                render: (status) => (
                  <Tag color={status === 'ACTIVE' ? 'success' : 'error'}>
                    {status === 'ACTIVE' ? '启用' : status === 'BANNED' ? '禁用' : status}
                  </Tag>
                ),
              },
              {
                title: '操作',
                key: 'action',
                render: (_, record) => (
                  <Switch 
                    checked={record.status === 'ACTIVE'} 
                    checkedChildren="启用"
                    unCheckedChildren="禁用"
                  />
                ),
              },
            ]}
            rowKey="operator"
            pagination={false}
          />
        </Card>
      ),
    },
    {
      key: 'success-patterns',
      label: '成功模式',
      children: (
        <Card className="glass-card">
          <Table
            columns={knowledgeColumns}
            dataSource={successPatterns || []}
            rowKey="id"
            loading={patternsLoading}
            pagination={{ pageSize: 20, showSizeChanger: true, showTotal: (t) => `共 ${t} 条` }}
          />
        </Card>
      ),
    },
    {
      key: 'knowledge-library',
      label: '因子知识库',
      children: <KnowledgeLibraryTab />,
    },
    {
      key: 'failure-pitfalls',
      label: '失败教训',
      children: (
        <Card className="glass-card">
          <Table
            columns={knowledgeColumns}
            dataSource={failurePitfalls || []}
            rowKey="id"
            loading={pitfallsLoading}
            pagination={{ pageSize: 20, showSizeChanger: true, showTotal: (t) => `共 ${t} 条` }}
          />
        </Card>
      ),
    },
  ]

  return (
    <div>
      <Title level={3} style={{ marginBottom: 24 }}>
        <SettingOutlined style={{ marginRight: 12, color: '#00d4ff' }} />
        配置中心
      </Title>

      <Tabs items={tabs} size="large" defaultActiveKey="credentials" />
    </div>
  )
}


// Knowledge Library browser. Lists all SUCCESS_PATTERN KB entries with
// filters (source / region) and inline edit (confidence + soft-delete via
// is_active toggle). Tier filter retired post tier-system removal (2026-05-18).

function KnowledgeLibraryTab() {
  const queryClient = useQueryClient()
  const [filters, setFilters] = useState({
    source: undefined,
    region: undefined,
    only_active: true,
  })
  const [detailModal, setDetailModal] = useState(null)

  const { data, isLoading } = useQuery({
    queryKey: ['knowledge-library', filters],
    queryFn: async () => {
      const params = {
        entry_type: 'SUCCESS_PATTERN',
        limit: 200,
      }
      if (filters.only_active) params.is_active = true
      if (filters.source) params.created_by = filters.source
      if (filters.region) params.region = filters.region
      const resp = await api.getKnowledgeEntries(params)
      return Array.isArray(resp) ? resp : resp.items || []
    },
  })

  const filtered = data || []

  const updateMutation = useMutation({
    mutationFn: ({ id, updates }) => api.updateKnowledgeEntry(id, updates),
    onSuccess: () => {
      message.success('已更新')
      queryClient.invalidateQueries({ queryKey: ['knowledge-library'] })
    },
    onError: (e) => message.error(`更新失败: ${e.message}`),
  })

  const deactivateMutation = useMutation({
    mutationFn: (id) => api.updateKnowledgeEntry(id, { is_active: false }),
    onSuccess: () => {
      message.success('已停用')
      queryClient.invalidateQueries({ queryKey: ['knowledge-library'] })
    },
  })

  const columns = [
    {
      title: '模式',
      dataIndex: 'pattern',
      ellipsis: true,
      width: 320,
      render: (p) => (
        <Tooltip title={p}>
          <Text code style={{ fontSize: 12 }}>
            {p?.length > 60 ? `${p.slice(0, 60)}...` : p}
          </Text>
        </Tooltip>
      ),
    },
    {
      title: '来源',
      dataIndex: 'created_by',
      width: 90,
      render: (s) => {
        const label = s === 'HITL' ? '人工' : s === 'USER' ? '用户' : '系统'
        return (
          <Tag color={s === 'HITL' ? 'gold' : s === 'USER' ? 'blue' : 'default'}>
            {label}
          </Tag>
        )
      },
    },
    {
      title: '地区',
      width: 80,
      render: (_, row) => row.meta_data?.region || '—',
    },
    {
      title: '数据集',
      width: 130,
      ellipsis: true,
      render: (_, row) =>
        row.meta_data?.dataset_id || row.meta_data?.dataset || '—',
    },
    {
      title: '置信度',
      width: 110,
      render: (_, row) => {
        const c = row.meta_data?.confidence
        return c != null ? c.toFixed(2) : '—'
      },
    },
    {
      title: '使用次数',
      dataIndex: 'usage_count',
      width: 70,
    },
    {
      title: '启用',
      dataIndex: 'is_active',
      width: 80,
      render: (a, row) => (
        <Switch
          size="small"
          checked={a}
          onChange={(checked) =>
            updateMutation.mutate({ id: row.id, updates: { is_active: checked } })
          }
        />
      ),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 130,
      render: (t) =>
        t ? (
          <Text type="secondary" style={{ fontSize: 11 }}>
            {new Date(t).toLocaleDateString()}
          </Text>
        ) : (
          '—'
        ),
    },
    {
      title: '操作',
      width: 130,
      render: (_, row) => (
        <Space size={4}>
          <Button size="small" onClick={() => setDetailModal(row)}>
            详情
          </Button>
          {row.is_active && (
            <Button
              size="small"
              danger
              onClick={() => deactivateMutation.mutate(row.id)}
            >
              停用
            </Button>
          )}
        </Space>
      ),
    },
  ]

  return (
    <Card className="glass-card">
      <Space style={{ marginBottom: 12 }} wrap>
        <Text>来源:</Text>
        <Select
          allowClear
          placeholder="全部"
          style={{ width: 130 }}
          value={filters.source}
          onChange={(v) => setFilters((f) => ({ ...f, source: v }))}
          options={[
            { value: 'SYSTEM', label: '系统' },
            { value: 'HITL', label: '人工' },
            { value: 'USER', label: '用户' },
          ]}
        />
        <Text>地区:</Text>
        <Select
          allowClear
          placeholder="全部"
          style={{ width: 110 }}
          value={filters.region}
          onChange={(v) => setFilters((f) => ({ ...f, region: v }))}
          options={['USA', 'CHN', 'EUR', 'ASI', 'GLB'].map((r) => ({
            value: r,
            label: r,
          }))}
        />
        <Switch
          checked={filters.only_active}
          onChange={(v) => setFilters((f) => ({ ...f, only_active: v }))}
        />
        <Text>仅显示启用</Text>
      </Space>
      <Table
        rowKey="id"
        size="small"
        columns={columns}
        dataSource={filtered}
        loading={isLoading}
        pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 条` }}
        scroll={{ x: 1200 }}
      />
      {detailModal && (
        <KnowledgeDetailModal
          entry={detailModal}
          onClose={() => setDetailModal(null)}
          onSaveConfidence={(id, confidence) =>
            updateMutation.mutate({
              id,
              updates: {
                meta_data: {
                  ...(detailModal.meta_data || {}),
                  confidence,
                },
              },
            })
          }
        />
      )}
    </Card>
  )
}


function KnowledgeDetailModal({ entry, onClose, onSaveConfidence }) {
  const meta = entry.meta_data || {}
  const [confidence, setConfidence] = useState(meta.confidence ?? 0.5)
  return (
    <Modal
      open
      title={`知识条目 #${entry.id}`}
      onCancel={onClose}
      onOk={() => {
        onSaveConfidence(entry.id, confidence)
        onClose()
      }}
      width={720}
    >
      <Descriptions bordered size="small" column={1}>
        <Descriptions.Item label="模式">
          <Text code style={{ wordBreak: 'break-all' }}>{entry.pattern}</Text>
        </Descriptions.Item>
        <Descriptions.Item label="描述">
          {entry.description || '—'}
        </Descriptions.Item>
        <Descriptions.Item label="地区 / 数据集">
          {meta.region || '—'} / {meta.dataset_id || meta.dataset || '—'}
        </Descriptions.Item>
        <Descriptions.Item label="来源">
          {entry.created_by === 'HITL' ? '人工' : entry.created_by === 'USER' ? '用户' : '系统'}
        </Descriptions.Item>
        <Descriptions.Item label="使用次数 / 是否启用">
          {entry.usage_count} / {entry.is_active ? '是' : '否'}
        </Descriptions.Item>
        <Descriptions.Item label="关联 alpha ID">
          {meta.alpha_id_ref ?? '—'}
        </Descriptions.Item>
        <Descriptions.Item label="置信度">
          <InputNumber
            value={confidence}
            min={0}
            max={1}
            step={0.05}
            onChange={(v) => setConfidence(v)}
          />
          <Text type="secondary" style={{ marginLeft: 8 }}>
            （确定后保存为该条目的置信度）
          </Text>
        </Descriptions.Item>
        <Descriptions.Item label="完整元数据">
          <Paragraph style={{ marginBottom: 0 }}>
            <code style={{ fontSize: 11 }}>
              {JSON.stringify(meta, null, 2)}
            </code>
          </Paragraph>
        </Descriptions.Item>
      </Descriptions>
    </Modal>
  )
}
