import { useEffect, useState } from 'react';
import { fetchHealth, fetchSummary, type Summary } from './api';

type RequestState<T> =
  | { status: 'loading' }
  | { status: 'success'; data: T }
  | { status: 'error'; message: string };

const countFormat = new Intl.NumberFormat('ru-RU');
const dateFormat = new Intl.DateTimeFormat('ru-RU', { timeZone: 'UTC' });
const displayDate = (date: string) => dateFormat.format(new Date(`${date}T00:00:00Z`));
const errorMessage = (error: unknown) => error instanceof Error ? error.message : 'Не удалось получить данные.';

export default function App() {
  const [attempt, setAttempt] = useState(0);
  const [health, setHealth] = useState<RequestState<void>>({ status: 'loading' });
  const [summary, setSummary] = useState<RequestState<Summary>>({ status: 'loading' });

  useEffect(() => {
    const controller = new AbortController();
    setHealth({ status: 'loading' });
    setSummary({ status: 'loading' });

    // Independent requests start together; health remains visible if summary fails.
    void fetchHealth(controller.signal).then(
      () => { if (!controller.signal.aborted) setHealth({ status: 'success', data: undefined }); },
      (error: unknown) => { if (!controller.signal.aborted) setHealth({ status: 'error', message: errorMessage(error) }); },
    );
    void fetchSummary(controller.signal).then(
      (data) => { if (!controller.signal.aborted) setSummary({ status: 'success', data }); },
      (error: unknown) => { if (!controller.signal.aborted) setSummary({ status: 'error', message: errorMessage(error) }); },
    );

    return () => controller.abort();
  }, [attempt]);

  const loading = health.status === 'loading' || summary.status === 'loading';
  const hasError = health.status === 'error' || summary.status === 'error';

  return (
    <main className="shell">
      <header className="header">
        <div>
          <p className="eyebrow">Аналитика транзакционной сети</p>
          <h1>Fusion</h1>
        </div>
        <span className="local-label">Локальный проект</span>
      </header>

      <section className="overview" aria-labelledby="overview-title">
        <div className="section-heading">
          <div>
            <h2 id="overview-title">Обзор данных</h2>
            <p className="subtitle">Сводка по загруженной выгрузке переводов.</p>
          </div>
          <button type="button" disabled={loading} onClick={() => setAttempt((value) => value + 1)}>
            {loading ? 'Загрузка…' : hasError ? 'Повторить' : 'Обновить'}
          </button>
        </div>

        <p className={`backend-status ${health.status}`} role="status">
          <span className="status-dot" aria-hidden="true" />
          {health.status === 'loading' ? 'Сервер: проверка соединения…' :
            health.status === 'success' ? 'Сервер подключён' : 'Сервер недоступен'}
        </p>

        {health.status === 'error' ? <p className="error-detail">{health.message}</p> : null}

        <div aria-live="polite" aria-busy={summary.status === 'loading'}>
          {summary.status === 'loading' ? (
            <p className="notice">Загружаем размеры и период данных…</p>
          ) : summary.status === 'error' ? (
            <div className="error-panel" role="alert">
              <h3>Не удалось загрузить сводку</h3>
              <p>{summary.message}</p>
              <p>Убедитесь, что локальный сервер запущен, и нажмите «Повторить».</p>
            </div>
          ) : (
            <>
              <dl className="metrics">
                <div><dt>Узлы</dt><dd>{countFormat.format(summary.data.nodes)}</dd></div>
                <div><dt>Направленные рёбра</dt><dd>{countFormat.format(summary.data.edges)}</dd></div>
                <div><dt>Транзакции</dt><dd>{countFormat.format(summary.data.transactions)}</dd></div>
                <div><dt>Исходные узлы (seed)</dt><dd>{countFormat.format(summary.data.seed_nodes)}</dd></div>
              </dl>
              <div className="period">
                <span>Период данных</span>
                <strong>
                  <time dateTime={summary.data.period.start}>{displayDate(summary.data.period.start)}</time>
                  {' — '}
                  <time dateTime={summary.data.period.end}>{displayDate(summary.data.period.end)}</time>
                </strong>
              </div>
            </>
          )}
        </div>
      </section>

      <footer>
        Выгрузка отражает только наблюдаемые переводы. Она не описывает полный баланс счетов.
      </footer>
    </main>
  );
}
