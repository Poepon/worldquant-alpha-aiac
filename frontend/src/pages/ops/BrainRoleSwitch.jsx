import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Descriptions,
  Empty,
  Modal,
  Row,
  Space,
  Spin,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  ReloadOutlined,
  SwapOutlined,
  ArrowRightOutlined,
} from '@ant-design/icons'
import api from '../../services/api'
import { formatDateTime, formatRelative } from '../../utils/time'

const { Title, Text, Paragraph } = Typography

// CONSULTANT mode targets — sourced from CLAUDE.md §"P3-Brain" + plan §14.
// effective_sharpe_submit_min = max(SHARPE_MIN, 1.58), testPeriod = P0Y,
// regions = 5 (USA/CHN/HKG/JPN/EUR). These are documentation constants,
// not fetched live — the backend state endpoint only reports the *current*
// mode's effective values, so the inactive column shows the spec.
const CONSULTANT_TARGETS = {
  sharpe_submit_min: '≥ 1.58 (max with env SHARPE_MIN)',
  test_period: 'P0Y',
  region_count: '5 (USA / CHN / HKG / JPN / EUR)',
}

const USER_TARGETS = {
  sharpe_submit_min: 'env SHARPE_MIN (default 1.25)',
  test_period: 'env default',
  region_count: 'limited (typically USA only)',
}

function ModeColumn({ label, color, isCurrent, targets, state, showCurrent }) {
  return (
    <Card
      size="small"
      className="glass-card"
      title={
        <Space>
          <Tag color={color}>{label}</Tag>
          {isCurrent && <Tag color="processing">当前</Tag>}
        </Space>
      }
      style={{ height: '100%' }}
    >
      <Descriptions column={1} size="small">
        <Descriptions.Item label="Sharpe 提交门槛">
          {isCurrent && showCurrent ? (
            <Text strong>{state.effective_sharpe_submit_min}</Text>
          ) : (
            <Text type="secondary">{targets.sharpe_submit_min}</Text>
          )}
        </Descriptions.Item>
        <Descriptions.Item label="testPeriod">
          {isCurrent && showCurrent ? (
            <Text strong>{state.effective_default_test_period}</Text>
          ) : (
            <Text type="secondary">{targets.test_period}</Text>
          )}
        </Descriptions.Item>
        <Descriptions.Item label="可用 region">
          {isCurrent && showCurrent ? (
            <Space wrap>
              {Object.entries(state.effective_region_universes).map(([r, u]) => (
                <Tag key={r}>{`${r}/${u}`}</Tag>
              ))}
            </Space>
          ) : (
            <Text type="secondary">{targets.region_count}</Text>
          )}
        </Descriptions.Item>
      </Descriptions>
    </Card>
  )
}

export default function BrainRoleSwitch() {
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
      message.error(`加载 BRAIN role state 失败：${e?.response?.data?.detail || e.message}`)
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
          ? '已切回 USER 模式（running task 的 Sharpe/testPeriod 不变；multi-sim/PROD-corr 立即降级）'
          : `已切到 CONSULTANT 模式${result?.sync_enqueued ? '（后台全球数据同步已触发）' : ''}`,
      )
      setModalOpen(false)
      await fetchState()
    } catch (e) {
      message.error(`切换失败：${e?.response?.data?.detail || e.message}`)
    } finally {
      setSwitching(false)
    }
  }

  if (loading && !state) {
    return (
      <div style={{ textAlign: 'center', padding: 80 }}>
        <Spin size="large" />
      </div>
    )
  }

  if (!state) {
    return <Empty description="无 state 数据" />
  }

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          <SwapOutlined style={{ marginRight: 8 }} />
          BRAIN 模式
        </Title>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={fetchState} loading={loading}>
            刷新
          </Button>
          <Button
            type={isConsultant ? 'default' : 'primary'}
            danger={isConsultant}
            icon={<ArrowRightOutlined />}
            onClick={openSwitchModal}
          >
            {isConsultant ? '切回 USER 模式' : '切换到 CONSULTANT 模式'}
          </Button>
        </Space>
      </Space>

      <Alert
        type={isConsultant ? 'warning' : 'info'}
        showIcon
        style={{ marginBottom: 16 }}
        message={
          <Space>
            <span>当前模式：</span>
            <Tag color={isConsultant ? 'gold' : 'green'}>{state.mode}</Tag>
            {state.last_switched_at && (
              <Text type="secondary">
                上次切换 {formatRelative(state.last_switched_at)}（{formatDateTime(state.last_switched_at)}） by {state.last_switched_by || '—'}
              </Text>
            )}
            <Text type="secondary">· 运行中 task {state.running_tasks_count} 个（快照已冻结，不受切换影响）</Text>
          </Space>
        }
      />

      <Title level={5}>USER vs CONSULTANT 能力对比</Title>
      <Paragraph type="secondary" style={{ marginTop: 0 }}>
        当前模式列显示 backend 实时返回的 effective_* 值；对面列显示文档约定（plan §14）。切换前请核对差异是否符合预期。
      </Paragraph>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={24} md={12}>
          <ModeColumn
            label="USER"
            color="green"
            isCurrent={!isConsultant}
            targets={USER_TARGETS}
            state={state}
            showCurrent
          />
        </Col>
        <Col xs={24} md={12}>
          <ModeColumn
            label="CONSULTANT"
            color="gold"
            isCurrent={isConsultant}
            targets={CONSULTANT_TARGETS}
            state={state}
            showCurrent
          />
        </Col>
      </Row>

      <Card
        className="glass-card"
        title="切换语义（Direction-C，plan §14）"
        size="small"
      >
        <ul style={{ marginBottom: 0 }}>
          <li>
            <Text strong>数据一致性能力</Text>（Sharpe 阈值 / testPeriod）走任务启动快照 —
            running task <b>不受切换影响</b>，新发起的 task 用新值
          </li>
          <li>
            <Text strong>endpoint 选择能力</Text>（multi-sim / PROD-corr 第 3 门 gate）走全局
            settings —— 切换 <b>立即生效</b>，避免 USER 状态调用 Consultant API
          </li>
          <li>
            <Text strong>安全网</Text>：若 BRAIN 在 submit 时返回 PROD-corr 403（账号实际未授权），
            系统自动切回 USER 并写 audit
          </li>
        </ul>
      </Card>

      <Modal
        title={isConsultant ? '切回 USER 模式' : '切换到 CONSULTANT 模式'}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={handleConfirmSwitch}
        okButtonProps={{ disabled: !acknowledged, loading: switching, danger: isConsultant }}
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
            <Paragraph>切换到 CONSULTANT 模式后：</Paragraph>
            <ul>
              <li>立即触发后台 5 region 同步（USA/CHN/HKG/JPN/EUR，预计 10-30 分钟）</li>
              <li>新发起 task 使用 testPeriod=P0Y、Sharpe 提交门槛抬到 1.58</li>
              <li>
                当前 {state.running_tasks_count} 个 running task <b>不受影响</b>（读启动时冻结的配置）
              </li>
              <li>
                <Text type="warning">
                  <b>注意 legacy alpha</b>：task_id=NULL（v5 之前创建）的旧 alpha 在下次 sync 时会被用 Sharpe=1.58 重判 →
                  可能批量 PASS → PASS_PROVISIONAL 降级。建议切换前先回填 task_id，或接受此一次性降级。
                </Text>
              </li>
              <li>
                <b>安全网</b>：若 BRAIN 在下次 submit_alpha 时返回 PROD-corr 403（账号实际未授权），
                系统会<b>自动切回 USER</b> 并写 audit 日志
              </li>
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
            <Paragraph>切回 USER 模式后：</Paragraph>
            <ul>
              <li>
                已在跑的 Consultant task 的 <b>Sharpe 门槛、testPeriod 设置</b> 仍按启动时配置（数据一致性保留）
              </li>
              <li>
                <b>Multi-simulation 立即降级为 single-sim 循环</b>
                （吞吐率下降 ~10-30x；若 task 还有大量 alpha 待 sim，evaluate 时间会显著拉长）
              </li>
              <li>
                <b>PROD-correlation 第 3 门 gate 立即停跑</b>
                ；该 task 后续提交的 alpha 只过 self_corr precheck — BRAIN 服务端可能在 submit 时拒
              </li>
              <li>
                如需完全停止 task，在任务详情页用 &quot;intervene&quot; 操作收尾
                （/api/v1/tasks/&#123;id&#125;/intervene → status=COMPLETED）
              </li>
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
    </div>
  )
}
