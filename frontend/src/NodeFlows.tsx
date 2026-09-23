import { useState } from 'react';
import type { Gid, GraphData, NodeDetail } from './api';
import { count } from './presentation';
import './node-flows.css';

type FlowEdge = GraphData['edges'][number];
type Direction = 'incoming' | 'outgoing';
interface Props { node: NodeDetail; onSelect: (gid: Gid) => void }
const flowMoney = (value: number) => `${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 }).format(value)} ₸`;

function FlowDetails({ node, onSelect }: Props) {
  const [direction, setDirection] = useState<Direction>(node.incoming_edges.length > 0 ? 'incoming' : 'outgoing');
  const incoming = direction === 'incoming';
  const counterpart = (edge: FlowEdge) => incoming ? edge.src : edge.dst;
  // The API sorts full flows by amount and exact integer IDs; keep that order.
  const edges = incoming ? node.incoming_edges : node.outgoing_edges;
  const counterparties = new Set(edges.map(counterpart).filter(gid => gid !== node.gid)).size;
  const totalKzt = edges.reduce((sum, edge) => sum + edge.sum_kzt, 0);
  const transactions = edges.reduce((sum, edge) => sum + edge.n_tx, 0);
  const hasSelfTransfer = edges.some(edge => edge.src === edge.dst);
  const isolated = node.incoming_edges.length === 0 && node.outgoing_edges.length === 0;
  const titleId = `flows-title-${node.gid}`;

  return <details className="node-flows">
    <summary>
      <span className="node-flows-summary-title" id={titleId}>Денежные потоки участника</span>
      <span className="node-flows-summary-count">{isolated ? 'Нет наблюдаемых связей' : `${count(node.incoming_edges.length)} входящих · ${count(node.outgoing_edges.length)} исходящих связей`}</span>
    </summary>
    <div className="node-flows-body">
      <p className="node-flows-subject">Выбранный ID <strong>{node.gid}</strong></p>
      <p className="node-flows-scope">Все связи этого участника в наблюдаемой выгрузке. Ограничение графа в 250 узлов не сокращает этот список.</p>
      <div className="node-flows-direction" role="group" aria-label="Направление денежных потоков">
        <button type="button" aria-pressed={incoming} onClick={() => setDirection('incoming')}>Входящие <span>{count(node.incoming_edges.length)}</span></button>
        <button type="button" aria-pressed={!incoming} onClick={() => setDirection('outgoing')}>Исходящие <span>{count(node.outgoing_edges.length)}</span></button>
      </div>
      <dl className="node-flows-totals">
        <div><dt>{incoming ? 'Получено в выгрузке' : 'Отправлено в выгрузке'}</dt><dd>{flowMoney(totalKzt)}</dd></div>
        <div><dt>Переводов</dt><dd>{count(transactions)}</dd></div>
        <div><dt>{incoming ? 'Плательщиков' : 'Получателей'}</dt><dd>{count(counterparties)}</dd></div>
      </dl>
      {edges.length > 0 ? <div className="node-flows-scroll" tabIndex={0} role="region" aria-label={`${incoming ? 'Входящие' : 'Исходящие'} переводы участника ${node.gid}`}>
        <table className="node-flows-table" aria-labelledby={titleId}>
          <caption>{incoming ? 'Отправитель → выбранный участник' : 'Выбранный участник → получатель'}. Суммы по убыванию.</caption>
          <thead><tr><th scope="col">{incoming ? 'Отправитель' : 'Получатель'}</th><th scope="col">Сумма, KZT</th><th scope="col">Переводов</th></tr></thead>
          <tbody>{edges.map(edge => <tr key={`${edge.src}:${edge.dst}`}>
            <td><button className="node-flow-gid" type="button" onClick={() => onSelect(counterpart(edge))} aria-label={`Открыть участника ${counterpart(edge)}, ${incoming ? 'отправитель' : 'получатель'}`}>{counterpart(edge)}</button>
              {edge.src === edge.dst && <span className="node-flow-self">Перевод самому себе</span>}</td>
            <td>{flowMoney(edge.sum_kzt)}</td><td>{count(edge.n_tx)}</td>
          </tr>)}</tbody>
        </table>
      </div> : <p className="node-flows-empty" role="status">{isolated
        ? 'В выгрузке нет переводов этого участника. Он сохранён в графе и результатах; отсутствие связей не доказывает отсутствие активности.'
        : incoming ? 'Наблюдаемых входящих переводов нет. Проверьте исходящие связи и ограничения выборки.'
          : node.truncated_by_depth ? 'Исходящие связи обрезаны границей наблюдения на глубине 4. Нельзя заключить, что деньги остались у участника.'
            : 'Наблюдаемых исходящих переводов нет. Это не подтверждает отсутствие переводов за пределами выборки.'}</p>}
      {hasSelfTransfer && <p className="node-flows-note">Перевод самому себе включён в суммы и число переводов, но не добавляет контрагента. Такая связь присутствует в обоих направлениях.</p>}
      <p className="node-flows-note">Показаны агрегаты направленных пар за период выгрузки, только внутрибанковские переводы от 5 000 KZT. Это не полный баланс счёта.</p>
    </div>
  </details>;
}

export default function NodeFlows(props: Props) {
  // Reset direction and disclosure on a different participant; never retain
  // another participant's flow list while the parent selection changes.
  return <FlowDetails key={props.node.gid} {...props} />;
}
