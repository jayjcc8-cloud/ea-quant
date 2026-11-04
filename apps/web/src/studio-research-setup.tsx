import { useState, type ReactNode } from 'react'
import { SourceLink as Link } from './workspace-navigation'
import type { ScenarioSummary } from './app'

type Dataset = { dataset_id: string; origin?: 'registered' | 'upload'; record_count?: number; replay_start_utc?: string; replay_end_utc?: string }
const behavior: Record<string, { name: string; description: string }> = {
  'bounded-long-v1': { name: '延迟后买入一次', description: '等待指定根数的行情后，发出一次买入信号。' },
  'moving-average-entry-v1': { name: '快均线高于慢均线时买入一次', description: '快均线严格高于慢均线时发出一次买入信号，不要求刚刚上穿。' },
}
const strategyName = (item: ScenarioSummary) => behavior[item.summary?.strategy_id ?? '']?.name ?? item.strategy_descriptor?.display_name ?? item.name

/** The studio presents the existing binding choice; its caller owns all reads and writes. */
export function StudioResearchSetup({ scenarios, selected, candidate, selectScenario, datasetId, selectDataset, datasets, datasetsReady,
  datasetError, catalogReady, catalogError, reloadCatalog, importHref, criterion, busy, bind }: {
  scenarios: ScenarioSummary[]; selected: string; candidate: ScenarioSummary | null; selectScenario: (id: string) => void;
  datasetId: string; selectDataset: (id: string) => void; datasets: Dataset[]; datasetsReady: boolean; datasetError: string;
  catalogReady: boolean; catalogError: string | null; reloadCatalog: () => void; importHref: string;
  criterion: ReactNode; busy: boolean; bind: () => void;
}) {
  const [mode, setMode] = useState<'own' | 'registered'>(() => selected && !selected.startsWith('template:') ? 'registered' : 'own')
  const templates = scenarios.filter(item => item.scenario_id.startsWith('template:'))
  const registered = scenarios.filter(item => !item.scenario_id.startsWith('template:'))
  // Only an explicit upload origin identifies the user's published CSV. A registered
  // input has no reliable synthetic/real flag in this catalog and is not relabelled.
  const uploads = datasets.filter(item => item.origin === 'upload')
  const dataset = uploads.find(item => item.dataset_id === datasetId)
  const chosenBehavior = candidate && behavior[candidate.summary?.strategy_id ?? '']
  const instrument = candidate?.summary ? `${candidate.summary.venue}:${candidate.summary.symbol}` : ''
  const selectMode = (next: 'own' | 'registered') => { setMode(next); selectScenario('') }
  return <section className="studio-research-setup" aria-label="策略与数据准备">
    <header><h2>为这项研究选择策略与数据</h2><p>先选一种当前支持的策略，再选择要研究的数据。</p></header>
    <div className="setup-mode" role="group" aria-label="数据来源">
      <button type="button" aria-pressed={mode === 'own'} disabled={busy} onClick={() => selectMode('own')}>使用自己的 CSV</button>
      <button type="button" aria-pressed={mode === 'registered'} disabled={busy} onClick={() => selectMode('registered')}>已登记输入</button>
    </div>
    {!catalogReady && !catalogError && <p role="status">正在读取可用策略…</p>}
    {catalogError && <p className="notice error" role="alert">{catalogError}</p>}
    {mode === 'own' ? <>
      <fieldset disabled={busy || !catalogReady}><legend>1. 选择策略</legend>
        <div className="setup-options">{templates.map(item => <label className="setup-option" key={item.scenario_id}>
          <input type="radio" name="studio-setup-strategy" value={item.scenario_id} checked={selected === item.scenario_id} onChange={() => selectScenario(item.scenario_id)} />
          <span><strong>{strategyName(item)}</strong><small>{behavior[item.summary?.strategy_id ?? '']?.description ?? item.strategy_descriptor?.action_contract ?? '行为以该策略的登记说明为准。'}</small><small>{item.summary?.venue}:{item.summary?.symbol}</small></span>
        </label>)}</div>
        {catalogReady && !templates.length && <p>暂无可用策略。请重新读取，或查看已登记输入。</p>}
      </fieldset>
      <fieldset disabled={busy}><legend>2. 选择数据</legend>
        {!candidate ? <p className="muted">选好策略后，这里会显示兼容的已发布 CSV。</p> : <>
          {!datasetsReady && <p role="status">正在读取已发布数据…</p>}
          {datasetError && <p className="notice error" role="alert">{datasetError}</p>}
          {datasetsReady && !datasetError && !uploads.length && <p>没有可用于 {instrument} 的已发布 CSV。导入后可返回这里继续。</p>}
          {!!uploads.length && <div className="field"><label htmlFor="studio-setup-dataset">已发布的 CSV 数据</label>
            <select id="studio-setup-dataset" value={datasetId} onChange={event => selectDataset(event.target.value)}>
              <option value="">请选择一个数据版本</option>
              {uploads.map(item => <option key={item.dataset_id} value={item.dataset_id}>{item.dataset_id} · {item.record_count ?? '未记录'} 条</option>)}
            </select>
            <small>仅列出与 {instrument} 标的一致的数据。</small>
          </div>}
          <Link className="button" to={importHref}>导入 CSV</Link>
        </>}
      </fieldset>
    </> : <fieldset disabled={busy || !catalogReady}><legend>选择已登记输入</legend>
      <p className="muted">使用工作区已登记的策略与原有数据。数据性质以各输入的来源说明为准。</p>
      <div className="field"><label htmlFor="studio-setup-registered">已登记输入</label>
        <select id="studio-setup-registered" value={selected} onChange={event => selectScenario(event.target.value)}>
          <option value="">请选择已登记输入</option>
          {registered.map(item => <option value={item.scenario_id} key={item.scenario_id}>{item.name} · {strategyName(item)} · {item.summary?.venue}:{item.summary?.symbol}</option>)}
        </select>
      </div>
      {catalogReady && !registered.length && <p>暂无已登记输入。可以使用自己的 CSV 开始研究。</p>}
    </fieldset>}
    {(!catalogReady || catalogError || (mode === 'own' ? !templates.length : !registered.length)) && <button type="button" className="text-button" disabled={busy || (!catalogReady && !catalogError)} onClick={reloadCatalog}>重新读取策略</button>}
    {candidate?.summary && <section className="setup-summary" aria-label="选择摘要"><h3>本次选择</h3>
      <dl><div><dt>策略</dt><dd>{strategyName(candidate)}</dd></div><div><dt>标的</dt><dd>{instrument}</dd></div>
        <div><dt>数据</dt><dd>{mode === 'own' ? dataset?.dataset_id ?? '尚未选择' : candidate.name}</dd></div>
        <div><dt>记录数</dt><dd>{mode === 'own' ? dataset?.record_count ?? '尚未选择' : candidate.summary.record_count}</dd></div>
      </dl>
      {chosenBehavior && <p>当前策略只发出一次买入信号，无自动卖出规则；期末可能仍持有仓位。</p>}
      <details><summary>查看策略设定与来源</summary><p>登记标识：{candidate.scenario_id} · {candidate.summary.strategy_id}</p>
        <p>初始资金：{candidate.summary.initial_cash}{candidate.template_assumptions?.instrument.settlement_currency ? ` ${candidate.template_assumptions.instrument.settlement_currency}` : ''}</p>
        {candidate.template_assumptions && <pre>{JSON.stringify(candidate.template_assumptions, null, 2)}</pre>}
        {mode === 'registered' && candidate.summary.data && <p>数据区间：{candidate.summary.data.start_utc} — {candidate.summary.data.end_utc}</p>}
      </details>
    </section>}
    <details className="setup-criterion"><summary>如何判断结果（可选）</summary><p className="muted">可以先不设置，回测结果仍会完整保存。</p>{criterion}</details>
    <div className="setup-footer"><p>更换策略需新建研究；参数和数据之后可调整。此次选择只生成草稿，不运行回测。</p>
      <button type="button" className="primary button" disabled={busy || !catalogReady || !candidate || (mode === 'own' && !dataset)} onClick={bind}>{busy ? '正在保存选择…' : '使用此策略与数据'}</button>
    </div>
  </section>
}
