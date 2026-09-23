import type { NodeDetail } from './api';
import './priority-explanation.css';

const points = new Intl.NumberFormat('ru-RU', {
  minimumFractionDigits: 6,
  maximumFractionDigits: 6,
});
const factors = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 6 });
const counts = new Intl.NumberFormat('ru-RU');

const contributions = [
  { key: 'priority_pagerank', label: 'Связи · PageRank' },
  { key: 'priority_turnover', label: 'Наблюдаемый оборот' },
  { key: 'priority_seed_reach', label: 'Достижимость seed' },
  { key: 'priority_counterparties', label: 'Контрагенты' },
  { key: 'priority_transactions', label: 'Переводы' },
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
    <div className="priority-explanation-heading">
      <h3 id={headingId}>Почему такой приоритет</h3>
      <p className="priority-position" aria-label={`Место ${node.priority_rank} из ${node.priority_total} участников`}>
        <strong>№{counts.format(node.priority_rank)}</strong> из {counts.format(node.priority_total)}
      </p>
    </div>

    <ul className="priority-contributions" aria-label="Пять взвешенных вкладов в приоритет">
      {contributions.map(({ key, label }) => <li key={key}>
        <span className="priority-contribution-label">{label}</span>
        <div className="priority-contribution-track" aria-hidden="true">
          <span style={{ width: `${Math.max(0, Math.min(1, node[key])) * 100}%` }} />
        </div>
        <strong className="priority-contribution-value">+{points.format(node[key])}</strong>
      </li>)}
    </ul>
    <p className="priority-scale-note">Вклады с весами · общая шкала полос 0–1.</p>

    <div className="priority-equation" aria-label={`База ${points.format(node.priority_base)} умножить на ${factors.format(node.priority_factor)} равно ${points.format(node.priority_score)}`}>
      <div><span>База</span><strong>{points.format(node.priority_base)}</strong></div>
      <span className="priority-operator" aria-hidden="true">×</span>
      <div><span>Множитель</span><strong>{factors.format(node.priority_factor)}</strong></div>
      <span className="priority-operator" aria-hidden="true">=</span>
      <div><span>Приоритет</span><strong>{points.format(node.priority_score)}</strong></div>
    </div>
    <details className="priority-why">
      <summary>Обоснование позиции</summary>
      <p>{node.priority_why}</p>
      <p className="priority-rounding-note">Округление до 6 знаков может дать разницу в последнем знаке.</p>
    </details>

    <details className={`priority-coverage ${coverage}`}>
      <summary><span className="priority-coverage-badge">{coverageLabel}</span>
        {node.is_seed && <span className="priority-seed-badge">Вход seed неполон</span>}
      </summary>
      {coverage === 'boundary' && <p>Приоритет уменьшен множителем {factors.format(node.priority_factor)} из-за неполноты исходящих. Это политика модели, не доказательство низкой важности.</p>}
      {coverage === 'isolated' && <p>Приоритет обнулён: в выгрузке нет связей. Активность вне выборки неизвестна.</p>}
      <p>Только внутрибанковская выборка от 5 000 KZT; внешние потоки не видны.</p>
      {node.is_seed && <p className="priority-seed-note">Вход seed неполон: out/in не является полным балансом.</p>}
    </details>
  </section>;
}
