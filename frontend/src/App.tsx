import { useEffect, useState, type CSSProperties, type FormEvent } from 'react';
import {
  fetchAnalysis, fetchGraph, fetchHealth, fetchNode,
  type Analysis, type Gid, type GraphData, type GraphScope, type NodeDetail, type Role,
} from './api';
import NetworkGraph from './NetworkGraph';
import AiPanel from './AiPanel';
import NodeFlows from './NodeFlows';
import PriorityExplanation from './PriorityExplanation';
import ClusterSummary from './ClusterSummary';
import { clusterColor, count, money, roleInfo, score } from './presentation';

type Focus = { gid: Gid; showNeighbors: boolean; attempt: number };
const errorMessage = (error: unknown) => error instanceof Error ? error.message : 'Не удалось получить данные.';
const dateFormat = new Intl.DateTimeFormat('ru-RU', { timeZone: 'UTC' });
const displayDate = (date: string) => dateFormat.format(new Date(`${date}T00:00:00Z`));
const colorStyle = (color: string) => ({ '--role-color': color } as CSSProperties);

function RoleBadge({ role }: { role: Role }) {
  return <span className="role-badge" style={colorStyle(roleInfo[role].color)}>{roleInfo[role].label}</span>;
}

export default function App() {
  const [attempt, setAttempt] = useState(0);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [analysisError, setAnalysisError] = useState('');
  const [health, setHealth] = useState<'loading' | 'ok' | 'error'>('loading');
  const [focus, setFocus] = useState<Focus | null>(null);
  const [node, setNode] = useState<NodeDetail | null>(null);
  const [nodeLoading, setNodeLoading] = useState(false);
  const [nodeError, setNodeError] = useState('');
  const [scope, setScope] = useState<GraphScope | null>(null);
  const [graph, setGraph] = useState<GraphData | null>(null);
  const [graphError, setGraphError] = useState('');
  const [graphAttempt, setGraphAttempt] = useState(0);
  const [graphLoading, setGraphLoading] = useState(false);
  const [search, setSearch] = useState('');
  const [searchError, setSearchError] = useState('');
  const [colorBy, setColorBy] = useState<'role' | 'cluster'>('role');

  useEffect(() => {
    const controller = new AbortController();
    setAnalysis(null);
    setAnalysisError('');
    setHealth('loading');
    setFocus(null);
    setScope(null);
    void fetchHealth(controller.signal).then(
      () => { if (!controller.signal.aborted) setHealth('ok'); },
      () => { if (!controller.signal.aborted) setHealth('error'); },
    );
    void fetchAnalysis(controller.signal).then(data => {
      if (controller.signal.aborted) return;
      setAnalysis(data);
      const first = data.top_nodes[0];
      if (first) setFocus({ gid: first.gid, showNeighbors: true, attempt: 0 });
      else if (data.clusters[0]) setScope({ cluster_id: data.clusters[0].cluster_id });
    }).catch(error => {
      if (!controller.signal.aborted) setAnalysisError(errorMessage(error));
    });
    return () => controller.abort();
  }, [attempt]);

  useEffect(() => {
    const controller = new AbortController();
    setNode(null);
    setNodeError('');
    setNodeLoading(Boolean(focus));
    if (focus) {
      void fetchNode(focus.gid, controller.signal).then(data => {
        if (controller.signal.aborted) return;
        setNode(data);
        setNodeLoading(false);
        if (focus.showNeighbors) setScope({ gid: data.gid });
      }).catch(error => {
        if (controller.signal.aborted) return;
        setNodeError(errorMessage(error));
        setNodeLoading(false);
      });
    }
    return () => controller.abort();
  }, [focus]);

  useEffect(() => {
    const controller = new AbortController();
    setGraph(null);
    setGraphError('');
    setGraphLoading(Boolean(scope));
    if (scope) {
      void fetchGraph(scope, controller.signal).then(data => {
        if (controller.signal.aborted) return;
        setGraph(data);
        setGraphLoading(false);
      }).catch(error => {
        if (controller.signal.aborted) return;
        setGraphError(errorMessage(error));
        setGraphLoading(false);
      });
    }
    return () => controller.abort();
  }, [scope, graphAttempt]);

  function selectNode(gid: Gid, showNeighbors: boolean) {
    setSearchError('');
    setFocus(previous => ({ gid, showNeighbors, attempt: (previous?.attempt ?? 0) + 1 }));
  }

  function searchNode(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const gid = search.trim();
    if (!/^-?(0|[1-9]\d*)$/.test(gid)) {
      setSearchError('Введите полный ID участника: целое число без пробелов.');
      return;
    }
    selectNode(gid, true);
  }

  function changeScope(value: string) {
    if (value === 'node') {
      const gid = node?.gid ?? (scope && 'gid' in scope ? scope.gid : null);
      if (gid) setScope({ gid });
      return;
    }
    setFocus(null);
    setScope({ cluster_id: Number(value) });
  }

  const selected = node && focus?.gid === node.gid ? node.gid : null;
  const detail = selected ? node : null;
  const currentCluster = scope && 'cluster_id' in scope
    ? analysis?.clusters.find(cluster => cluster.cluster_id === scope.cluster_id) : null;
  const displayedCluster = currentCluster ?? (detail ? analysis?.clusters.find(cluster => cluster.cluster_id === detail.cluster_id) : null);
  const visibleClusters = graph ? [...new Set(graph.nodes.map(item => item.cluster_id))].sort((a, b) => a - b) : [];

  return <main className="app-shell">
    <a className="skip-link" href="#network-title">Перейти к графу</a>
    <header className="app-header">
      <div className="brand" aria-label="Fusion — анализ финансовой сети">Fusion</div>
      <span className="product-badge">AML Network Intelligence</span>
      <span className={`connection ${health}`} role="status" title={health === 'ok' ? 'Локальный сервер подключён' : health === 'loading' ? 'Подключение…' : 'Сервер недоступен'}>
        <span aria-hidden="true">●</span><span>{health === 'ok' ? 'Подключено' : health === 'loading' ? 'Подключение…' : 'Нет соединения'}</span>
      </span>
      <form className="search-form" onSubmit={searchNode}>
        <label className="sr-only" htmlFor="gid-search">Полный ID участника</label>
        <input id="gid-search" inputMode="numeric" autoComplete="off" placeholder="Поиск GID…" value={search} onChange={event => { setSearch(event.target.value); setSearchError(''); }} aria-invalid={Boolean(searchError)} aria-describedby={searchError ? 'search-error' : undefined} />
        <button type="submit">Найти</button>
        {searchError && <p id="search-error" className="inline-error" role="alert">{searchError}</p>}
      </form>
      {analysis && <details className="export-menu">
        <summary>Экспорт CSV <span aria-hidden="true">↓</span></summary>
        <nav className="export-actions" aria-label="Скачать полные результаты">
          <a href="/api/exports/nodes_roles.csv" download>Все участники ↓</a>
          <a href="/api/exports/clusters.csv" download>Кластеры ↓</a>
          <a href="/api/exports/top_nodes.csv" download>Топ-20 ↓</a>
        </nav>
      </details>}
    </header>

    <h1 className="sr-only">Кого проверить первым? Анализ финансовой сети</h1>
    {!analysis && !analysisError && <div className="loading-panel app-loading" role="status">Загружаем результаты расчёта…</div>}
    {analysisError && <section className="error-panel app-error" role="alert"><h2>Не удалось загрузить результаты</h2><p>{analysisError}</p>
      <button className="primary" onClick={() => setAttempt(value => value + 1)}>Повторить</button></section>}

    {analysis && <>
      <div className="workspace">
        <section className="panel ranking" aria-labelledby="ranking-title">
          <div className="panel-heading"><h2 id="ranking-title">Приоритет проверки</h2><span className="count-pill">Top {analysis.top_nodes.length}</span></div>
          <div className="rank-list-scroll">
            <ol className="rank-list">
              {analysis.top_nodes.map(item => <li key={item.gid}>
                <button className={`rank-item ${selected === item.gid ? 'active' : ''}`} onClick={() => selectNode(item.gid, true)}
                  title={`ID ${item.gid}. ${item.why}`} aria-pressed={selected === item.gid} aria-label={`Участник ${item.gid}, ${roleInfo[item.role].label}, приоритет ${score(item.priority_score)}`}>
                  <span className="rank-number">#{item.rank}</span>
                  <span className="rank-person"><strong>{item.gid}</strong><RoleBadge role={item.role} /></span>
                  <span className="rank-score">{score(item.priority_score)}</span>
                </button>
              </li>)}
            </ol>
          </div>
          <p className="ranking-note">Приоритет задаёт порядок проверки, а не вероятность нарушения.</p>
        </section>

        <section className="panel network" aria-labelledby="network-title">
          <h2 id="network-title" className="sr-only" tabIndex={-1}>Граф переводов</h2>
          <dl className="metrics" aria-label="Сводка всей сети">
            <div><dt>Узлы</dt><dd>{count(analysis.nodes)}</dd></div>
            <div><dt>Связи</dt><dd>{count(analysis.edges)}</dd></div>
            <div><dt>Транзакции</dt><dd>{count(analysis.transactions)}</dd></div>
            <div><dt>Seed</dt><dd>{count(analysis.seed_nodes)}</dd></div>
            <div><dt>Кластеры</dt><dd>{count(analysis.clusters_count)}</dd></div>
            <div title={money(analysis.total_kzt)}><dt>Оборот</dt><dd>{new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(analysis.total_kzt / 1_000_000)} млн ₸</dd></div>
          </dl>
          <div className="graph-toolbar">
            <label><span className="sr-only">Область графа</span><select aria-label="Область графа" value={scope && 'cluster_id' in scope ? String(scope.cluster_id) : 'node'} onChange={event => changeScope(event.target.value)}>
              <option value="node" disabled={!node && !(scope && 'gid' in scope)}>Окружение участника</option>
              {analysis.clusters.map(cluster => <option key={cluster.cluster_id} value={String(cluster.cluster_id)}>Кластер {cluster.cluster_id} · {count(cluster.n_nodes)} участников</option>)}
            </select></label>
            <div className="color-toggle" role="group" aria-label="Окраска графа">
              <button aria-pressed={colorBy === 'role'} onClick={() => setColorBy('role')}>По ролям</button>
              <button aria-pressed={colorBy === 'cluster'} onClick={() => setColorBy('cluster')}>По кластерам</button>
            </div>
          </div>
          <div className="graph-stage">
            <div className="graph-area" aria-busy={graphLoading}>
              {graphLoading && <div className="loading-panel" role="status">Строим граф выбранной области…</div>}
              {graphError && <div className="error-panel" role="alert"><p>{graphError}</p><button onClick={() => setGraphAttempt(value => value + 1)}>Повторить граф</button></div>}
              {graph && <NetworkGraph graph={graph} selected={selected} colorBy={colorBy} onSelect={gid => selectNode(gid, false)} />}
              {!graph && !graphLoading && !graphError && <div className="empty-state">Выберите участника или кластер.</div>}
            </div>
            <div className="graph-caption">
              <div className="legend" aria-label="Легенда графа">
                {colorBy === 'role' ? Object.entries(roleInfo).map(([role, info]) => <span key={role}><i style={{ background: info.color }} aria-hidden="true" />{info.label}</span>)
                  : visibleClusters.map(id => <span key={id}><i style={{ background: clusterColor(id) }} aria-hidden="true" />Кластер {id}</span>)}
              </div>
              <p className="shape-key">→ направление · размер: приоритет · ◇ граница · двойной контур: seed</p>
              {graph && <><p className="graph-summary">{graph.scope}. Показано {count(graph.returned_nodes)} из {count(graph.total_nodes)} участников и {count(graph.returned_edges)} из {count(graph.total_edges)} связей.</p>
                {graph.truncated && <p className="limit-note">Показаны 250 участников по приоритету. CSV содержат полный результат.</p>}
                {graph.nodes.length === 1 && graph.edges.length === 0 && <p className="limit-note">В этой области нет наблюдаемых связей. Узел сохранён в результатах.</p>}
              </>}
              {graph && <details className="accessible-nodes"><summary>Выбрать участника из списка ({count(graph.returned_nodes)})</summary><div>
                {graph.nodes.map(item => <button key={item.gid} onClick={() => selectNode(item.gid, false)} aria-pressed={selected === item.gid}>{item.gid} · {roleInfo[item.role].label}</button>)}
              </div></details>}
            </div>
          </div>
          <div className="network-period"><span>Период наблюдения</span> <time dateTime={analysis.period.start}>{displayDate(analysis.period.start)}</time> — <time dateTime={analysis.period.end}>{displayDate(analysis.period.end)}</time></div>
        </section>

        <aside className="panel detail" aria-labelledby="detail-title">
          <h2 id="detail-title" className="sr-only">Карточка участника</h2>
          <div className="detail-body" key={focus?.gid ?? 'empty'} aria-live="polite" aria-busy={nodeLoading}>
            {nodeLoading && <div className="loading-panel" role="status">Загружаем признаки участника…</div>}
            {nodeError && <div className="error-panel" role="alert"><p>{nodeError}</p><p className="small">Запрошен ID: {focus?.gid}</p><button onClick={() => setFocus(previous => previous ? { ...previous, attempt: previous.attempt + 1 } : null)}>Повторить карточку</button></div>}
            {!detail && !nodeLoading && !nodeError && <div className="empty-state">Нажмите на участника в графе или найдите его по GID.</div>}
            {detail && <>
              <section className="node-overview">
                <div className="identity"><span className="eyebrow">GID</span><strong>{detail.gid}</strong></div>
                <div className="score-overview">
                  <div className="score-value"><strong>{score(detail.priority_score)}</strong><span>аналитический приоритет · #{count(detail.priority_rank)}</span></div>
                  <div className="role-overview"><RoleBadge role={detail.role} /><span>Выраженность {score(detail.role_score)}</span></div>
                </div>
                <div className="tags"><span>Кластер {detail.cluster_id}</span><span>Глубина {detail.depth}</span>{detail.is_seed && <span>Seed</span>}{detail.truncated_by_depth && <span className="boundary-tag">Граница выгрузки</span>}{detail.is_isolated && <span>Нет связей</span>}</div>
                {detail.priority_rank <= 20 && detail.priority_score > 0 && <p className="analyst-recommendation">Кандидат для дополнительной AML-проверки</p>}
              </section>
              <PriorityExplanation node={detail} />
              <NodeFlows node={detail} onSelect={gid => selectNode(gid, true)} />
            </>}
            {displayedCluster && <ClusterSummary cluster={displayedCluster} onSelect={gid => selectNode(gid, true)}
              onShow={() => setScope({ cluster_id: displayedCluster.cluster_id })} showingCluster={Boolean(currentCluster)} />}
            {detail && <>
              <details className="card-section role-details"><summary>Почему эта роль</summary><p className="evidence">{detail.evidence}</p>
                <p className="role-strength">Выраженность признаков роли <strong>{score(detail.role_score)}</strong></p>
                <p className="muted small">Поддержка выбранного правила, не вероятность. Значения разных ролей не калиброваны между собой.</p>
              </details>
              <details className="card-section facts-details"><summary>Все признаки участника</summary><dl className="node-facts">
                <div><dt>Получено</dt><dd>{money(detail.in_kzt)}</dd></div><div><dt>Отправлено</dt><dd>{money(detail.out_kzt)}</dd></div>
                <div><dt>Плательщиков</dt><dd>{count(detail.in_deg)}</dd></div><div><dt>Получателей</dt><dd>{count(detail.out_deg)}</dd></div>
                <div><dt>Входящих переводов</dt><dd>{count(detail.in_tx)}</dd></div><div><dt>Исходящих переводов</dt><dd>{count(detail.out_tx)}</dd></div>
                <div><dt>Достижим от seed</dt><dd>{count(detail.seed_reach)}</dd></div><div><dt>PageRank</dt><dd>{detail.pagerank.toPrecision(4)}</dd></div>
                <div><dt>Отправлено / получено</dt><dd>{detail.pass_through === null ? 'Не определено' : detail.pass_through.toFixed(3)}</dd></div>
              </dl><p className="muted small">{detail.ratio_usable ? 'Отношение отражает только наблюдаемые потоки, не полный баланс.' : 'Отношение out/in не используется для определения транзита и консолидации у этого узла.'}</p></details>
              <details className="card-section observation-details"><summary>Все ограничения наблюдения</summary><ul className="caveats">{detail.caveats.map(caveat => <li key={caveat}>{caveat}</li>)}</ul></details>
              <div className="detail-actions"><AiPanel key={detail.gid} gid={detail.gid} />
                <button className="full-width" onClick={() => setScope({ gid: detail.gid })}>Показать ближайшие связи</button>
              </div>
            </>}
          </div>
        </aside>
      </div>
      <footer>Fusion помогает проверять гипотезы. Выгрузка не описывает полный баланс счетов; выводы требуют проверки аналитиком.</footer>
    </>}
  </main>;
}
