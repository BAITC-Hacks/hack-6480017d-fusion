import type { NodeDetail } from './api';
import './priority-explanation.css';

const points = new Intl.NumberFormat('ru-RU', {
  minimumFractionDigits: 6,
  maximumFractionDigits: 6,
});
const factors = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 6 });
const counts = new Intl.NumberFormat('ru-RU');

const contributions = [
  { key: 'priority_pagerank', label: 'Связи · PageRank', color: '#297d83' },
  { key: 'priority_turnover', label: 'Наблюдаемый оборот', color: '#567bb0' },
  { key: 'priority_seed_reach', label: 'Достижимость от seed', color: '#6b8050' },
  { key: 'priority_counterparties', label: 'Число контрагентов', color: '#ad783f' },
  { key: 'priority_transactions', label: 'Число переводов', color: '#738392' },
] as const;

export default function PriorityExplanation({ node }: { node: NodeDetail }) {
  const headingId = `priority-explanation-${node.gid}`;
  const coverage = node.is_isolated ? 'isolated' : node.truncated_by_depth ? 'boundary' : 'observed';
  const coverageLabel = {
    isolated: 'Нет наблюдаемых связей',
    boundary: 'Граница depth=4',
    observed: 'Не обрезан на depth=4',
  }[coverage];

  return <section className="priority-explanation" aria-labelledby={headingId}>
    <h3 id={headingId}>Почему такой приоритет</h3>
    <p className="priority-position">Место <strong>{counts.format(node.priority_rank)}</strong> из {counts.format(node.priority_total)} участников</p>

    <ul className="priority-contributions" aria-label="Пять взвешенных вкладов в приоритет">
      {contributions.map(({ key, label, color }) => <li key={key}>
        <div className="priority-contribution-value"><span>{label}</span><strong>{points.format(node[key])}</strong></div>
        <div className="priority-contribution-track" aria-hidden="true">
          <span style={{ width: `${Math.max(0, Math.min(1, node[key])) * 100}%`, backgroundColor: color }} />
        </div>
      </li>)}
    </ul>
    <p className="priority-scale-note">Вклады уже учитывают веса. Общая шкала полос: 0–1 балл.</p>

    <div className="priority-equation" aria-label={`База ${points.format(node.priority_base)} умножить на ${factors.format(node.priority_factor)} равно ${points.format(node.priority_score)}`}>
      <div><span>База</span><strong>{points.format(node.priority_base)}</strong></div>
      <span className="priority-operator" aria-hidden="true">×</span>
      <div><span>Множитель</span><strong>{factors.format(node.priority_factor)}</strong></div>
      <span className="priority-operator" aria-hidden="true">=</span>
      <div><span>Приоритет</span><strong>{points.format(node.priority_score)}</strong></div>
    </div>
    <p className="priority-rounding-note">Округление до 6 знаков может дать разницу в последнем знаке.</p>

    <details className="priority-why">
      <summary>Обоснование позиции</summary>
      <p>{node.priority_why}</p>
    </details>

    <div className={`priority-coverage ${coverage}`}>
      <span className="priority-coverage-badge">{coverageLabel}</span>
      {coverage === 'boundary' && <p>Приоритет уменьшен множителем {factors.format(node.priority_factor)} из-за неполноты исходящих. Это политика модели, не доказательство низкой важности.</p>}
      {coverage === 'isolated' && <p>Приоритет обнулён: в выгрузке нет связей. Активность вне выборки неизвестна.</p>}
      <p>Только внутрибанковская выборка от 5 000 KZT; внешние потоки не видны.</p>
      {node.is_seed && <p className="priority-seed-note">Вход seed неполон: out/in не является полным балансом.</p>}
    </div>
  </section>;
}
