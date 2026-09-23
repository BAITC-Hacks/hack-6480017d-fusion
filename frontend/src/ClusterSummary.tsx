import type { Cluster, Gid } from './api';
import { count } from './presentation';
import './cluster-summary.css';

const amountFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 });

interface Props {
  cluster: Cluster;
  onSelect: (gid: Gid) => void;
  onShow: () => void;
  showingCluster: boolean;
}

export default function ClusterSummary({ cluster, onSelect, onShow, showingCluster }: Props) {
  return <section className="cluster-summary" aria-label={`Сводка кластера ${cluster.cluster_id}`}>
    <div className="cluster-summary-heading">
      <h3>Кластер {cluster.cluster_id}</h3>
      {!showingCluster && <button type="button" onClick={onShow}>Показать граф</button>}
    </div>
    <dl className="cluster-summary-metrics">
      <div><dt>Участников</dt><dd>{count(cluster.n_nodes)}</dd></div>
      <div><dt>Исходных seed</dt><dd>{count(cluster.n_seed)}</dd></div>
      <div><dt>Внутренний оборот</dt><dd>{amountFormat.format(cluster.sum_kzt_internal)} ₸</dd></div>
    </dl>
    <details>
      <summary>Гипотеза и лидеры · {cluster.top_gids.length}</summary>
      <p className="cluster-summary-hypothesis">{cluster.hypothesis}</p>
      <p className="cluster-summary-list-label">Участники по аналитическому приоритету</p>
      <ol>{cluster.top_gids.map(gid => <li key={gid}>
        <button type="button" onClick={() => onSelect(gid)} aria-label={`Открыть участника ${gid}`}>{gid}</button>
      </li>)}</ol>
    </details>
    <p className="cluster-summary-note">Только связи внутри кластера. Совместная деятельность не установлена.</p>
  </section>;
}
