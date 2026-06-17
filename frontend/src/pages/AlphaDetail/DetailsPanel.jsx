import { Typography, Descriptions, Space, Tag, Divider, Tooltip as AntTooltip } from 'antd'
import { formatRelativeUTC, formatDateTimeUTC } from '../../utils/time'

const { Text, Paragraph } = Typography

export default function DetailsPanel({ alpha }) {
  return (
    <div>
      {(alpha.hypothesis || alpha.logic_explanation) && (
        <>
          {alpha.hypothesis && (
            <>
              <Text strong>假设 (Hypothesis):</Text>
              <Paragraph style={{ color: 'rgba(255,255,255,0.85)' }}>{alpha.hypothesis}</Paragraph>
            </>
          )}
          {alpha.logic_explanation && (
            <>
              <Text strong>逻辑解释:</Text>
              <Paragraph style={{ color: 'rgba(255,255,255,0.85)' }}>{alpha.logic_explanation}</Paragraph>
            </>
          )}
          <Divider />
        </>
      )}
      <Descriptions column={1} size="small">
        <Descriptions.Item label="BRAIN Alpha ID">
          {alpha.alpha_id
            ? <Text code copyable={{ text: alpha.alpha_id, tooltips: ['复制', '已复制'] }}>{alpha.alpha_id}</Text>
            : <Text type="secondary">未提交至 BRAIN</Text>}
        </Descriptions.Item>
        <Descriptions.Item label="地区 / 股票池">{alpha.region} · {alpha.universe}</Descriptions.Item>
        <Descriptions.Item label="数据集">{alpha.dataset_id || '—'}</Descriptions.Item>
        <Descriptions.Item label="使用字段">
          <Space wrap size={[4, 4]}>
            {(alpha.fields_used || []).length ? alpha.fields_used.map((f) => <Tag key={f}>{f}</Tag>) : '—'}
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="使用算子">
          <Space wrap size={[4, 4]}>
            {(alpha.operators_used || []).length ? alpha.operators_used.map((o) => <Tag key={o} color="blue">{o}</Tag>) : '—'}
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="创建时间">
          <AntTooltip title={formatDateTimeUTC(alpha.created_at)}><span>{formatRelativeUTC(alpha.created_at)}</span></AntTooltip>
        </Descriptions.Item>
      </Descriptions>
    </div>
  )
}
