import type { AgentEvent, RunDetail } from '../types'
import { compactNumber, statusLabel } from '../helpers'
import { useLanguage } from '../i18n'

interface RunHeaderProps {
  run: RunDetail
  onRetry: () => void
  retrying: boolean
  onOpenSettings: () => void
}

function latestContext(events: AgentEvent[]) {
  return [...events].reverse().find((event) => event.context_tokens != null)?.context_tokens
}

function cny(value: number, locale: string): string {
  const digits = value < 0.01 ? 4 : value < 1 ? 3 : 2
  return `¥${new Intl.NumberFormat(locale, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value)}`
}

export function RunHeader({ run, onRetry, retrying, onOpenSettings }: RunHeaderProps) {
  const { language } = useLanguage()
  const en = language === 'en'
  const locale = en ? 'en-US' : 'zh-CN'
  const steps = Math.max(0, ...run.events.map((event) => event.step ?? 0))
  const subagents = new Set(run.events.filter((event) => event.subagent != null).map((event) => event.subagent)).size
  const context = latestContext(run.events)
  const cap = run.project_budget_settings?.revision_budget_limit_cny
    ?? run.project.settings?.revision_budget_limit_cny
    ?? 40
  const baseline = run.project.active_revision?.usage_baseline_cny ?? 0
  const spent = Math.max(0, (run.usage?.estimated_cost_cny ?? 0) - baseline)
  const budgetProgress = Math.min(100, cap > 0 ? (spent / cap) * 100 : 0)

  return (
    <header className="run-header">
      <div className="run-title-group">
        <div className="eyebrow">{en ? 'Mathematical modeling project' : '数学建模项目'}</div>
        <h1>{run.name}</h1>
      </div>
      <div className="header-spacer" />
      <div className="run-metrics">
        <div><strong>{steps}</strong><span>{en ? 'Steps' : '步数'}</span></div>
        <div><strong>{compactNumber(context)}</strong><span>{en ? 'Context' : '上下文'}</span></div>
        <div><strong>{subagents}</strong><span>{en ? 'Sub-agents' : '协作 Agent'}</span></div>
      </div>
      <button className="revision-budget-summary" type="button" onClick={onOpenSettings} title={en ? 'Set the revision cost cap' : '设置单次修改追加费用上限'}>
        <span><b>{en ? 'Revision budget' : '修改预算'}</b><em>{cny(spent, locale)} / {cny(cap, locale)}</em></span>
        <i><span style={{ width: `${budgetProgress}%` }} /></i>
      </button>
      {run.usage && run.usage.total_tokens > 0 && (
        <details className="usage-summary">
          <summary title={en ? 'View cached input, uncached input, output, and cost details' : '查看缓存输入、未缓存输入、输出和费用明细'}>
            <strong>
              {run.usage.pricing_complete
                ? cny(run.usage.estimated_cost_cny, locale)
                : run.usage.priced_tokens > 0
                  ? `${cny(run.usage.estimated_cost_cny, locale)}+`
                  : (en ? 'Cost pending' : '金额待统计')}
            </strong>
            <span>{en ? 'This project' : '本项目'}</span>
            <i>⌄</i>
          </summary>
          <div className="usage-popover">
            <header>
              <div><span>{en ? 'Token usage' : 'Token 用量'}</span><strong>{compactNumber(run.usage.total_tokens)}</strong></div>
              <small>
                {run.usage.pricing_complete
                  ? (en
                    ? 'Calculated from cache hits, model, and request time'
                    : '按缓存命中、模型与请求时间计算')
                  : (en ? 'Incomplete because some records or prices are unavailable' : '存在旧记录或未配置价格，金额不完整')}
              </small>
            </header>
            <dl>
              <div><dt>{en ? 'Cached input' : '缓存输入'}</dt><dd>{compactNumber(run.usage.cached_input_tokens)}</dd></div>
              <div><dt>{en ? 'Uncached input' : '未缓存输入'}</dt><dd>{compactNumber(run.usage.uncached_input_tokens)}</dd></div>
              <div><dt>{en ? 'Output' : '输出'}</dt><dd>{compactNumber(run.usage.completion_tokens)}</dd></div>
              {run.usage.unclassified_input_tokens > 0 && (
                <div className="usage-unclassified">
                  <dt>{en ? 'Unclassified input' : '未分类输入'}</dt>
                  <dd>{compactNumber(run.usage.unclassified_input_tokens)}</dd>
                </div>
              )}
            </dl>
            {run.usage.external_model_usage?.map((item) => (
              <section className="external-model-usage" key={`${item.tool}:${item.provider}:${item.model}`}>
                <div>
                  <strong>{item.model}</strong>
                  <span>{en ? 'Image analysis' : '图片理解'}</span>
                </div>
                <small>
                  {en ? 'Input' : '输入'} {compactNumber(item.prompt_tokens)} · {en ? 'Output' : '输出'} {compactNumber(item.completion_tokens)}
                </small>
                <b>{cny(item.estimated_cost_cny, locale)}</b>
              </section>
            ))}
            <footer>
              <span>{en ? 'Conversation cost' : '会话金额'}</span>
              <strong>
                {run.usage.pricing_complete
                  ? cny(run.usage.estimated_cost_cny, locale)
                  : run.usage.priced_tokens > 0
                    ? (en ? `${cny(run.usage.estimated_cost_cny, locale)} priced` : `已统计 ${cny(run.usage.estimated_cost_cny, locale)}`)
                    : (en ? 'No cost available' : '暂无可用金额')}
              </strong>
            </footer>
          </div>
        </details>
      )}
      <div className={`status-pill ${run.status}`}>
        <span className="status-dot" />
        {statusLabel(run.status, language)}
      </div>
      {(run.status === 'error' || run.status === 'stopped' || run.status === 'cancelled') && (
        <button
          className="retry-run-button"
          onClick={onRetry}
          disabled={retrying}
          title={en ? 'Restart with the same problem and materials' : '使用相同的问题和材料重新开始'}
        >
          {retrying ? (en ? 'Restarting…' : '正在重新开始…') : (en ? 'Restart' : '重新开始')}
        </button>
      )}
    </header>
  )
}
