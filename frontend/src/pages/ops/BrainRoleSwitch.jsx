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

// 模式代号 → 中文展示（CONSULTANT/USER 是后端返回的 key，不改 key 只加 label）
const MODE_LABEL = {
  CONSULTANT: '顾问模式',
  USER: '普通用户模式',
}

// CONSULTANT mode targets — sourced from CLAUDE.md §"P3-Brain" + plan §14.
// effective_sharpe_submit_min = max(SHARPE_MIN, 1.58), testPeriod = P0Y,
// regions = 5 (USA/CHN/HKG/JPN/EUR). These are documentation constants,
// not fetched live — the backend state endpoint only reports the *current*
// mode's effective values, so the inactive column shows the spec.
const CONSULTANT_TARGETS = {
  sharpe_submit_min: '≥ 1.58（取环境配置 SHARPE_MIN 与 1.58 中的较大值）',
  test_period: 'P0Y（全样本）',
  region_count: '5 个地区（美国 / 中国 / 香港 / 日本 / 欧洲）',
}

const USER_TARGETS = {
  sharpe_submit_min: '环境配置 SHARPE_MIN（默认 1.25）',
  test_period: '环境默认',
  region_count: '受限（通常仅美国）',
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
        <Descriptions.Item label="测试区间">
          {isCurrent && showCurrent ? (
            <Text strong>{state.effective_default_test_period}</Text>
          ) : (
            <Text type="secondary">{targets.test_period}</Text>
          )}
        </Descriptions.Item>
        <Descriptions.Item label="可用地区">
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
      message.error(`加载 BRAIN 账号模式状态失败：${e?.response?.data?.detail || e.message}`)
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
          ? '已切回普通用户模式（运行中任务的 Sharpe 门槛/测试区间不变；批量回测与生产相关性检查立即降级）'
          : `已切到顾问模式${result?.sync_enqueued ? '（后台全球数据同步已触发）' : ''}`,
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
    return <Empty description="暂无状态数据" />
  }

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          <SwapOutlined style={{ marginRight: 8 }} />
          BRAIN 账号模式
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
            {isConsultant ? '切回普通用户模式' : '切换到顾问模式'}
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
            <Tag color={isConsultant ? 'gold' : 'green'}>{MODE_LABEL[state.mode] || state.mode}</Tag>
            {state.last_switched_at && (
              <Text type="secondary">
                上次切换 {formatRelative(state.last_switched_at)}（{formatDateTime(state.last_switched_at)}） 操作人 {state.last_switched_by || '—'}
              </Text>
            )}
            <Text type="secondary">· 运行中任务 {state.running_tasks_count} 个（配置快照已冻结，不受切换影响）</Text>
          </Space>
        }
      />

      <Title level={5}>普通用户模式 vs 顾问模式 能力对比</Title>
      <Paragraph type="secondary" style={{ marginTop: 0 }}>
        当前模式列显示后端实时返回的生效值；对面列显示文档约定的目标值。切换前请核对差异是否符合预期。
      </Paragraph>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={24} md={12}>
          <ModeColumn
            label="普通用户模式"
            color="green"
            isCurrent={!isConsultant}
            targets={USER_TARGETS}
            state={state}
            showCurrent
          />
        </Col>
        <Col xs={24} md={12}>
          <ModeColumn
            label="顾问模式"
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
        title="切换行为说明"
        size="small"
      >
        <ul style={{ marginBottom: 0 }}>
          <li>
            <Text strong>数据检查能力</Text>（Sharpe 阈值 / 回测时间窗口）按任务启动时的快照执行 —
            正在运行的任务 <b>不受切换影响</b>，新创建的任务使用新值
          </li>
          <li>
            <Text strong>API 能力</Text>（批量模拟 / 生产相关性检查）走全局开关 —— 切换
            <b>立即生效</b>，避免在普通账号状态下误调用顾问级 API
          </li>
          <li>
            <Text strong>安全网</Text>：若 BRAIN 在提交 alpha 时返回 403（账号实际未升级），
            系统会自动切回普通模式并记录日志
          </li>
        </ul>
      </Card>

      <Modal
        title={isConsultant ? '切回普通用户模式' : '切换到顾问模式'}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={handleConfirmSwitch}
        okButtonProps={{ disabled: !acknowledged, loading: switching, danger: isConsultant }}
        okText={isConsultant ? '确认切回普通用户' : '确认切换到顾问'}
        cancelText="取消"
        width={640}
      >
        {!isConsultant ? (
          <>
            <Alert
              type="warning"
              showIcon
              message="请确认你已收到 BRAIN 平台的顾问账号升级邮件"
              style={{ marginBottom: 12 }}
            />
            <Paragraph>切换到顾问模式后：</Paragraph>
            <ul>
              <li>立即触发后台 5 个地区数据同步（美国 / 中国 / 香港 / 日本 / 欧洲，预计 10-30 分钟）</li>
              <li>新创建的任务使用 P0Y 回测窗口、Sharpe 提交门槛抬到 1.58</li>
              <li>
                当前正在运行的 {state.running_tasks_count} 个任务 <b>不受影响</b>（沿用启动时冻结的配置）
              </li>
              <li>
                <Text type="warning">
                  <b>注意历史 alpha</b>：早期未关联任务的 alpha 在下次同步时会用新 Sharpe 门槛 1.58 重新判定，
                  可能从『通过』降级为『暂定通过』。建议切换前先回填任务归属，或接受此一次性降级。
                </Text>
              </li>
              <li>
                <b>安全网</b>：若 BRAIN 在提交 alpha 时返回 403（账号实际未升级），
                系统会<b>自动切回普通模式</b>并记录日志
              </li>
            </ul>
          </>
        ) : (
          <>
            <Alert
              type="info"
              showIcon
              message="切回普通模式 — 应用层立即停止调用顾问级 BRAIN 接口"
              style={{ marginBottom: 12 }}
            />
            <Paragraph>切回普通模式后：</Paragraph>
            <ul>
              <li>
                正在运行的顾问任务 <b>仍沿用启动时的 Sharpe 门槛与回测窗口</b>（数据一致性保留）
              </li>
              <li>
                <b>批量模拟立即降级为单条循环</b>
                （吞吐率下降约 10-30 倍；若任务还有大量 alpha 待模拟，评估耗时会明显拉长）
              </li>
              <li>
                <b>生产相关性检查立即停用</b>
                ；后续提交的 alpha 只走本地的「与已提交策略相关度」预检 — BRAIN 服务端可能在提交时拒绝
              </li>
              <li>
                如需完全停止任务，请在任务详情页执行『干预』操作收尾（暂停 → 完成）
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
