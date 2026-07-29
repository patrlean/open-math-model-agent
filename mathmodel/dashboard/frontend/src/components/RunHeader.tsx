import type { AgentEvent, RunDetail } from '../types'
import { compactNumber, statusLabel } from '../helpers'

interface RunHeaderProps {
  run: RunDetail
  onRetry: () => void
  retrying: boolean
}

function latestContext(events: AgentEvent[]) {
  return [...events].reverse().find((event) => event.context_tokens != null)?.context_tokens
}

function cny(value: number): string {
  const digits = value < 0.01 ? 4 : value < 1 ? 3 : 2
  return `¥${new Intl.NumberFormat('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value)}`
}

export function RunHeader({ run, onRetry, retrying }: RunHeaderProps) {
  const steps = Math.max(0, ...run.events.map((event) => event.step ?? 0))
  const subagents = new Set(run.events.filter((event) => event.subagent != null).map((event) => event.subagent)).size
  const context = latestContext(run.events)

  return (
    <header className="run-header">
      <div className="run-title-group">
        <div className="eyebrow">建模会话</div>
        <h1>{run.name}</h1>
      </div>
      <div className="header-spacer" />
      <div className="run-metrics">
        <div><strong>{steps}</strong><span>步数</span></div>
        <div><strong>{compactNumber(context)}</strong><span>上下文</span></div>
        <div><strong>{subagents}</strong><span>协作 Agent</span></div>
      </div>
      {run.usage && run.usage.total_tokens > 0 && (
        <details className="usage-summary">
          <summary title="查看缓存输入、未缓存输入、输出和费用明细">
            <strong>
              {run.usage.pricing_complete
                ? cny(run.usage.estimated_cost_cny)
                : run.usage.priced_tokens > 0
                  ? `${cny(run.usage.estimated_cost_cny)}+`
                  : '金额待统计'}
            </strong>
            <span>本次会话</span>
            <i>⌄</i>
          </summary>
          <div className="usage-popover">
            <header>
              <div><span>Token 用量</span><strong>{compactNumber(run.usage.total_tokens)}</strong></div>
              <small>
                {run.usage.pricing_complete
                  ? '按实际缓存命中计算'
                  : '存在旧记录或未配置价格，金额不完整'}
              </small>
            </header>
            <dl>
              <div><dt>缓存输入</dt><dd>{compactNumber(run.usage.cached_input_tokens)}</dd></div>
              <div><dt>未缓存输入</dt><dd>{compactNumber(run.usage.uncached_input_tokens)}</dd></div>
              <div><dt>输出</dt><dd>{compactNumber(run.usage.completion_tokens)}</dd></div>
              {run.usage.unclassified_input_tokens > 0 && (
                <div className="usage-unclassified">
                  <dt>未分类输入</dt>
                  <dd>{compactNumber(run.usage.unclassified_input_tokens)}</dd>
                </div>
              )}
            </dl>
            <footer>
              <span>会话金额</span>
              <strong>
                {run.usage.pricing_complete
                  ? cny(run.usage.estimated_cost_cny)
                  : run.usage.priced_tokens > 0
                    ? `已统计 ${cny(run.usage.estimated_cost_cny)}`
                    : '暂无可用金额'}
              </strong>
            </footer>
          </div>
        </details>
      )}
      <div className={`status-pill ${run.status}`}>
        <span className="status-dot" />
        {statusLabel[run.status]}
      </div>
      {(run.status === 'error' || run.status === 'stopped' || run.status === 'cancelled') && (
        <button className="retry-run-button" onClick={onRetry} disabled={retrying} title="使用相同的问题和材料重新开始">
          {retrying ? '正在重新开始…' : '重新开始'}
        </button>
      )}
    </header>
  )
}
