import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { fetchCurrentDataset, resetDataset, uploadDataset, validateDatasetFiles, type DatasetInfo } from './datasetApi';
import './dataset.css';

interface Props { onChanged: () => void }
type Action = 'upload' | 'reset';
const number = new Intl.NumberFormat('ru-RU');
const fileSize = (size: number) => size < 1024 ? `${size} Б`
  : size < 1024 * 1024 ? `${(size / 1024).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} КиБ`
  : `${(size / (1024 * 1024)).toLocaleString('ru-RU', { maximumFractionDigits: 2 })} МиБ`;
const dateTime = (value: string) => Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('ru-RU') : value;

export default function DatasetPanel({ onChanged }: Props) {
  const id = useId();
  const [current, setCurrent] = useState<DatasetInfo | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [action, setAction] = useState<Action | null>(null);
  const [reading, setReading] = useState(false);
  const [readError, setReadError] = useState('');
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const openRef = useRef<HTMLButtonElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const readRef = useRef<AbortController | null>(null);
  const mutationRef = useRef(false);
  const mountedRef = useRef(false);
  const currentIdRef = useRef<string | null>(null);
  const nextPollRef = useRef(0);
  const validation = validateDatasetFiles(files);
  const busy = action !== null;

  const readCurrent = useCallback(async (interactive = false) => {
    if (mutationRef.current || readRef.current || (!interactive && Date.now() < nextPollRef.current)) return;
    const controller = new AbortController();
    readRef.current = controller;
    setReading(true);
    const timeout = window.setTimeout(() => controller.abort(), 10_000);
    try {
      const info = await fetchCurrentDataset(controller.signal);
      if (!mountedRef.current || controller.signal.aborted || readRef.current !== controller || mutationRef.current) return;
      const changed = currentIdRef.current !== null && currentIdRef.current !== info.id;
      currentIdRef.current = info.id;
      nextPollRef.current = 0;
      setCurrent(info);
      setReadError('');
      if (changed) {
        setSuccess('Текущий набор изменился. Расчёты приложения обновлены.');
        onChanged();
      }
    } catch (reason) {
      if (!mountedRef.current || readRef.current !== controller || mutationRef.current) return;
      nextPollRef.current = Date.now() + 30_000;
      setReadError(controller.signal.aborted ? 'Сервер не ответил вовремя. Можно обновить статус вручную.'
        : reason instanceof Error ? reason.message : 'Не удалось получить текущий набор.');
    } finally {
      window.clearTimeout(timeout);
      if (readRef.current === controller) {
        readRef.current = null;
        if (mountedRef.current) setReading(false);
      }
    }
  }, [onChanged]);

  useEffect(() => {
    mountedRef.current = true;
    const dialog = dialogRef.current;
    void readCurrent();
    const sync = () => { if (document.visibilityState === 'visible') void readCurrent(); };
    window.addEventListener('focus', sync);
    document.addEventListener('visibilitychange', sync);
    const interval = window.setInterval(sync, 10_000);
    return () => {
      mountedRef.current = false;
      readRef.current?.abort();
      readRef.current = null;
      window.clearInterval(interval);
      window.removeEventListener('focus', sync);
      document.removeEventListener('visibilitychange', sync);
      dialog?.close();
    };
  }, [readCurrent]);

  function open() {
    if (!dialogRef.current?.open) dialogRef.current?.showModal();
    void readCurrent(true);
  }

  async function changeDataset(nextAction: Action) {
    if (mutationRef.current) return;
    if (nextAction === 'upload' && validation) { setError(validation); return; }
    mutationRef.current = true;
    readRef.current?.abort();
    readRef.current = null;
    setReading(false);
    setAction(nextAction);
    setError('');
    setSuccess('');
    try {
      const info = await (nextAction === 'upload' ? uploadDataset(files) : resetDataset());
      if (!mountedRef.current) return;
      // Set the confirmed ID before notifying the app, so polling cannot notify it twice.
      currentIdRef.current = info.id;
      setCurrent(info);
      setReadError('');
      setFiles([]);
      if (inputRef.current) inputRef.current.value = '';
      setSuccess(`${nextAction === 'upload' ? 'Набор загружен' : 'Исходные данные восстановлены'}: ${number.format(info.nodes)} участников, ${number.format(info.edges)} связей, ${number.format(info.transactions)} переводов. Расчёты обновлены.`);
      onChanged();
    } catch (reason) {
      if (mountedRef.current) setError(reason instanceof Error ? reason.message : 'Не удалось сменить данные. Обновите статус и повторите попытку.');
    } finally {
      mutationRef.current = false;
      nextPollRef.current = 0;
      if (mountedRef.current) setAction(null);
    }
  }

  return <>
    <button ref={openRef} type="button" className="dataset-open" onClick={open} aria-haspopup="dialog" aria-label={busy ? 'Данные — обработка продолжается' : 'Данные'}>
      Данные{busy && <span className="dataset-busy-dot" aria-hidden="true" />}
    </button>
    <dialog ref={dialogRef} className="dataset-dialog" aria-labelledby={`${id}-title`} aria-describedby={`${id}-intro`}
      onClose={() => openRef.current?.focus({ preventScroll: true })}>
      <header className="dataset-dialog-header">
        <div><h2 id={`${id}-title`}>Данные Fusion</h2><p id={`${id}-intro`}>Загрузите набор по схеме финансового кейса.</p></div>
        <button type="button" className="dataset-close" onClick={() => dialogRef.current?.close()} autoFocus>Закрыть <span aria-hidden="true">×</span></button>
      </header>
      <div className="dataset-dialog-body">
        <section className="dataset-current" aria-labelledby={`${id}-current`}>
          <div className="dataset-section-heading"><h3 id={`${id}-current`}>Текущий набор</h3>
            <button type="button" className="dataset-text-button" disabled={busy || reading} onClick={() => void readCurrent(true)}>{reading ? 'Проверяем…' : 'Обновить статус'}</button>
          </div>
          {current ? <>
            <div className="dataset-current-name"><strong>{current.name}</strong><span>{current.is_default ? 'Исходный' : 'Загруженный'}</span></div>
            <dl className="dataset-stats">
              <div><dt>Участников</dt><dd>{number.format(current.nodes)}</dd></div>
              <div><dt>Связей</dt><dd>{number.format(current.edges)}</dd></div>
              <div><dt>Переводов</dt><dd>{number.format(current.transactions)}</dd></div>
            </dl>
            <p className="dataset-meta">Период: {current.period.start} — {current.period.end}</p>
            <p className="dataset-meta">Загружен: {dateTime(current.loaded_at)}</p>
          </> : <p className="dataset-note">{reading ? 'Получаем сведения о текущих данных…' : 'Сведения о текущем наборе пока недоступны. Можно выбрать файлы для загрузки.'}</p>}
          {readError && <p className="dataset-read-error" role="status">{readError}</p>}
        </section>

        <form className="dataset-upload" onSubmit={event => { event.preventDefault(); void changeDataset('upload'); }} aria-busy={busy}>
          <h3>Новый набор</h3>
          <p className="dataset-note">Сейчас ZIP или 3 Parquet по схеме кейса. CSV и Excel не поддерживаются.</p>
          <label className="dataset-picker-label" htmlFor={`${id}-files`}>Выберите один ZIP или три файла Parquet</label>
          <input ref={inputRef} id={`${id}-files`} className="dataset-picker" type="file" multiple accept=".parquet,.zip" disabled={busy}
            aria-describedby={`${id}-limits ${id}-selection`} onChange={event => {
              setFiles(Array.from(event.currentTarget.files ?? []));
              setError('');
              setSuccess('');
            }} />
          <p id={`${id}-limits`} className="dataset-note">До 50 МиБ суммарно; для ZIP — размер архива. После распаковки — до 200 МиБ. Внутри нужны nodes.parquet, edges.parquet и transactions.parquet.</p>
          {files.length > 0 && <ul className="dataset-file-list">{files.map((file, index) => <li key={`${file.name}-${index}`}><span>{file.name}</span><span>{fileSize(file.size)}</span></li>)}</ul>}
          <p id={`${id}-selection`} className={files.length > 0 && validation ? 'dataset-validation' : 'dataset-note'} aria-live="polite">
            {files.length === 0 ? 'Файлы ещё не выбраны.' : validation ?? 'Файлы выбраны. Схему и согласованность проверит сервер.'}
          </p>
          <div className="dataset-actions">
            <button type="submit" className="dataset-primary" disabled={busy || validation !== null}>{action === 'upload' ? 'Проверяем и рассчитываем…' : 'Проверить и загрузить'}</button>
            <button type="button" disabled={busy || current?.is_default === true} onClick={() => void changeDataset('reset')}>{action === 'reset' ? 'Восстанавливаем…' : 'Вернуть исходные данные'}</button>
          </div>
        </form>
        <div aria-live="polite" aria-atomic="true">
          {busy && <p className="dataset-progress">{action === 'upload' ? 'Проверяем файлы и пересчитываем граф, роли и кластеры.' : 'Возвращаем исходный набор.'} Окно можно закрыть — обработка продолжится.</p>}
          {success && <p className="dataset-success">{success}</p>}
        </div>
        {error && <p className="dataset-error" role="alert">{error}</p>}

        <details className="dataset-schema">
          <summary>Схема файлов и ограничения</summary>
          <dl>
            <div><dt>nodes.parquet</dt><dd><code>gid</code> — int64; <code>depth</code> — int64, 0–4; <code>is_seed</code> — bool.</dd></div>
            <div><dt>edges.parquet</dt><dd><code>src</code>, <code>dst</code> — int64; <code>sum_kzt</code> — float64; <code>n_tx</code> — int64; <code>depth</code> — int8, 1–4.</dd></div>
            <div><dt>transactions.parquet</dt><dd><code>src</code>, <code>dst</code> — int64; <code>date</code> — date32; <code>sum_kzt</code> — float64.</dd></div>
          </dl>
          <p>До 10 000 узлов, 100 000 связей и 200 000 транзакций. Та же модель наблюдения: обход по исходящим переводам до 4 колен, внутрибанковские переводы от 5 000 KZT. Период может отличаться от исходного; он определяется по датам транзакций. Входящие потоки seed и связи на глубине 4 могут быть неполными.</p>
          <p>Не меняйте идентификаторы на дробные числа. Суммы и число транзакций должны совпадать с агрегатами рёбер; все участники переводов должны присутствовать в nodes.parquet.</p>
        </details>
        <p className="dataset-scope">Текущий набор общий для всех вкладок Fusion на этом локальном сервере и сохраняется на сервере. Исходные файлы остаются неизменными.</p>
      </div>
    </dialog>
  </>;
}
