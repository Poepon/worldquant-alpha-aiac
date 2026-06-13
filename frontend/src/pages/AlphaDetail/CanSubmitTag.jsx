import { Tag, Tooltip as AntTooltip } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'

export default function CanSubmitTag({ canSubmit, failed, pending, loading, onRefresh }) {
  if (canSubmit === true) {
    const pendN = pending?.length || 0
    const tip = pendN > 0
      ? `提交前检查无一项未通过；仍有 ${pendN} 项待定（如「与已提交策略的相关度」）— 待定项通过后才是最终结论。`
      : '提交前检查全部通过 — 满足 BRAIN 提交门槛。'
    return (
      <AntTooltip title={tip}>
        <Tag color="success" icon={loading ? <ReloadOutlined spin /> : null} onClick={onRefresh} style={{ cursor: 'pointer' }}>
          ✅ 可提交{pendN > 0 ? ` (${pendN} 项待定)` : ''}
        </Tag>
      </AntTooltip>
    )
  }
  if (canSubmit === false) {
    const tip = (
      <div>
        <div style={{ marginBottom: 4 }}>{failed.length} 项 BRAIN 提交前检查未通过：</div>
        {failed.map((c) => (
          <div key={c.name} style={{ fontFamily: 'monospace', fontSize: 12 }}>
            • {c.name}
            {c.value !== undefined && c.limit !== undefined
              ? `（当前=${c.value}，门槛=${c.limit}）`
              : ''}
          </div>
        ))}
      </div>
    )
    return (
      <AntTooltip title={tip}>
        <Tag color="error" icon={loading ? <ReloadOutlined spin /> : null} onClick={onRefresh} style={{ cursor: 'pointer' }}>
          ⚠️ 不可提交 ({failed.length} 项未通过)
        </Tag>
      </AntTooltip>
    )
  }
  return (
    <AntTooltip title="尚未调用 BRAIN 做提交前检查，点击立即检查">
      <Tag onClick={onRefresh} style={{ cursor: 'pointer' }} icon={loading ? <ReloadOutlined spin /> : <ReloadOutlined />}>
        🔍 检查可提交性
      </Tag>
    </AntTooltip>
  )
}
