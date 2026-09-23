import { useState } from 'react';
import type { Gid, GraphData, NodeDetail } from './api';
import { count } from './presentation';
import './node-flows.css';

type FlowEdge = GraphData['edges'][number];
type Direction = 'incoming' | 'outgoing';
interface Props { node: NodeDetail; onSelect: (gid: Gid) => void }
const amountFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 });
const flowMoney = (value: number) => `${amountFormat.format(value)} ₸`;

function FlowDetails({ node, onSelect }: Props) {
  const [direction, setDirection] = useState<Direction>(node.incoming_edges.length > 0 ? 'incoming' : 'outgoing');
  const incoming = direction === 'incoming';
  const counterpart = (edge: FlowEdge) => incoming ? edge.src : edge.dst;
  // The API sorts full flows by amount and exact integer IDs; keep that order.
  const edges = incoming ? node.incoming_edges : node.outgoing_edges;
  const payers = new Set(node.incoming_edges.map(edge => edge.src).filter(gid => gid !== node.gid)).size;
  const recipients = new Set(node.outgoing_edges.map(edge => edge.dst).filter(gid => gid !== node.gid)).size;
  const receivedKzt = node.incoming_edges.reduce((sum, edge) => sum + edge.sum_kzt, 0);
  const sentKzt = node.outgoing_edges.reduce((sum, edge) => sum + edge.sum_kzt, 0);
  const transactions = edges.reduce((sum, edge) => sum + edge.n_tx, 0);
  const hasSelfTransfer = edges.some(edge => edge.src === edge.dst);
  const isolated = node.incoming_edges.length === 0 && node.outgoing_edges.length === 0;
  const titleId = `flows-title-${node.gid}`;

  return <section className="node-flows" aria-labelledby={titleId}>
    <h3 id={titleId}>Потоки</h3>
    <div className="node-flows-body">
      <dl className="node-flows-totals">
        <div><dt>Получено</dt><dd>{flowMoney(receivedKzt)}<span>Плательщиков: {count(payers)}</span></dd></div>
        <div><dt>Отправлено</dt><dd>{flowMoney(sentKzt)}<span>Получателей: {count(recipients)}</span></dd></div>
      </dl>
      <div className="node-flows-direction" role="group" aria-label="Направление денежных потоков">
        <button type="button" aria-pressed={incoming} onClick={() => setDirection('incoming')}>Входящие <span>{count(node.incoming_edges.length)}</span></button>
        <button type="button" aria-pressed={!incoming} onClick={() => setDirection('outgoing')}>Исходящие <span>{count(node.outgoing_edges.length)}</span></button>
      </div>
      <p className="node-flows-direction-total">{incoming ? 'Отправитель → этот участник' : 'Этот участник → получатель'} · {count(transactions)} переводов</p>
      {edges.length > 0 ? <div className="node-flows-scroll" tabIndex={0} role="region" aria-label={`${incoming ? 'Входящие' : 'Исходящие'} переводы участника ${node.gid}`}>
        <table className="node-flows-table" aria-labelledby={titleId}>
          <caption>{incoming ? 'Входящие' : 'Исходящие'} переводы участника {node.gid}. Суммы по убыванию.</caption>
          <thead><tr><th scope="col">{incoming ? 'ID отправителя' : 'ID получателя'}</th><th scope="col">Сумма</th><th scope="col" aria-label="Количество переводов">Опер.</th></tr></thead>
          <tbody>{edges.map(edge => <tr key={`${edge.src}:${edge.dst}`}>
            <td><button className="node-flow-gid" type="button" onClick={() => onSelect(counterpart(edge))} aria-label={`Открыть участника ${counterpart(edge)}, ${incoming ? 'отправитель' : 'получатель'}`}>{counterpart(edge)}</button>
              {edge.src === edge.dst && <span className="node-flow-self">Перевод самому себе</span>}</td>
            <td>{flowMoney(edge.sum_kzt)}</td><td>{count(edge.n_tx)}</td>
          </tr>)}</tbody>
        </table>
      </div> : <p className="node-flows-empty" role="status">{isolated
        ? 'В выгрузке нет переводов этого участника. Он сохранён в графе и результатах; отсутствие связей не доказывает отсутствие активности.'
        : incoming ? 'Наблюдаемых входящих переводов нет. Проверьте исходящие связи и ограничения выборки.'
          : node.truncated_by_depth ? 'Исходящие связи за границей наблюдения на глубине 4 неизвестны. Нельзя заключить, что деньги остались у участника.'
            : 'Наблюдаемых исходящих переводов нет. Это не подтверждает отсутствие переводов за пределами выборки.'}</p>}
      {hasSelfTransfer && <p className="node-flows-note">Перевод самому себе включён в суммы и число переводов, но не добавляет контрагента. Такая связь присутствует в обоих направлениях.</p>}
      <details className="node-flows-limits">
        <summary>Границы наблюдения</summary>
        <p>Все связи участника в наблюдаемой выгрузке; лимит графа в 250 узлов не сокращает список. Показаны агрегаты направленных пар за период выгрузки, только внутрибанковские переводы от 5 000 KZT. Это не полный баланс счёта.</p>
        {node.is_seed && <p>У исходных seed входящие связи могут быть неполными.</p>}
        {node.truncated_by_depth && <p>Граница наблюдения на глубине 4: входящие и исходящие связи могут быть неполными. Отсутствие исходящих переводов не означает, что деньги остались у участника.</p>}
      </details>
    </div>
  </section>;
}

export default function NodeFlows(props: Props) {
  // Reset direction on a different participant; never retain
  // another participant's flow list while the parent selection changes.
  return <FlowDetails key={props.node.gid} {...props} />;
}
