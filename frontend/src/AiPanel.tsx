import { useEffect, useRef, useState } from 'react';
import {
  fetchAiConfig, fetchAiJob, isReport, isReview, startAiJob,
  type AiConfig, type AiJob, type AiReport, type AiReview, type AiStage, type EvidencePacket,
} from './aiApi';
import './ai.css';

const statuses: Record<AiStage['status'], string> = {
  queued: 'В очереди', pending: 'Ожидает', running: 'Выполняется', success: 'Ответ API', cached: 'Из кеша', error: 'Ошибка', skipped: 'Пропущено',
};
const issueKinds: Record<string, string> = {
  unsupported_claim: 'Недостаточно оснований', missing_caveat: 'Не учтено ограничение',
  overstatement: 'Слишком сильная формулировка', contradiction: 'Возможное противоречие',
};
const formatNumber = new Intl.NumberFormat('ru-RU', { maximumSignificantDigits: 7 });
const formatMoney = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 });
const dateTime = (value: string) => Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('ru-RU') : value;
const evidenceElementId = (gid: string, id: string) => `ai-evidence-${gid}-${encodeURIComponent(id)}`;

function delay(signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = () => { window.clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')); };
    const timer = window.setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, 800);
    signal.addEventListener('abort', abort, { once: true });
    if (signal.aborted) abort();
  });
}

function TextList({ title, items }: { title: string; items: string[] }) {
  if (items.length === 0) return null;
  return <div className="ai-text-list"><h5>{title}</h5><ul>{[...new Set(items)].map(item => <li key={item}>{item}</li>)}</ul></div>;
}

function References({ ids, onEvidence }: { ids: string[]; onEvidence: (id: string) => void }) {
  return <span className="ai-references">{[...new Set(ids)].map(id =>
    <button key={id} type="button" className="ai-reference" onClick={() => onEvidence(id)} aria-label={`Показать источник ${id}`}>{id}</button>,
  )}</span>;
}

function StageHeader({ title, stage }: { title: string; stage: AiStage }) {
  const called = stage.elapsed_ms !== null;
  const cached = stage.status === 'cached';
  return <><div className="ai-stage-heading"><h4>{title}</h4><span className={`ai-status ${stage.status}`}>{statuses[stage.status]}</span></div>
    {(called || cached) && <p className="ai-call-meta">
      {stage.model && <span>{stage.model}</span>}
      {called && !cached && <span>{(stage.elapsed_ms! / 1000).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} с</span>}
      {stage.usage && <span title={`Вход: ${stage.usage.input_tokens ?? 'нет данных'}; выход: ${stage.usage.output_tokens ?? 'нет данных'}`}>
        {cached ? 'Исходный ответ: ' : ''}{stage.usage.total_tokens === null ? 'Провайдер не сообщил общее число токенов' : `${stage.usage.total_tokens.toLocaleString('ru-RU')} токенов`}</span>}
      {cached && <span>{stage.cached_at ? `Сохранён ${dateTime(stage.cached_at)}` : 'Сохранённый ответ API'} · без нового вызова</span>}
    </p>}
    {stage.error && <p className="ai-stage-error">{stage.error.message}</p>}
  </>;
}

function Report({ report, review, onEvidence }: { report: AiReport; review: AiReview | null; onEvidence: (id: string) => void }) {
  const questioned = new Set(review?.issues.map(item => item.claim_id).filter(id => id !== 'overall') ?? []);
  return <div className="ai-report">
    <p className="ai-hypothesis">{report.hypothesis}</p>
    {questioned.size > 0 && <p className="ai-review-key">Жёлтым отмечены утверждения с замечаниями рецензента. Замечания тоже требуют проверки.</p>}
    <ol className="ai-claims">{report.claims.map(claim => <li key={claim.claim_id} className={questioned.has(claim.claim_id) ? 'questioned' : ''}>
      <span className="ai-claim-id">{claim.claim_id}</span><p>{claim.text}</p>
      <References ids={claim.evidence_ids} onEvidence={onEvidence} />
      {questioned.has(claim.claim_id) && <span className="ai-claim-note">Есть замечание NVIDIA</span>}
    </li>)}</ol>
    <TextList title="Ограничения справки" items={report.limitations} />
    <TextList title="Что проверить дальше" items={report.next_checks} />
  </div>;
}

function Review({ review, onEvidence }: { review: AiReview; onEvidence: (id: string) => void }) {
  return <div className="ai-review">
    {review.issues.length === 0 ? <p className="ai-no-issues">Замечаний не найдено — это не подтверждение гипотезы.</p>
      : <ul className="ai-issues">{review.issues.map((issue, index) => <li key={`${issue.claim_id}-${issue.kind}-${index}`}>
        <strong>{issue.claim_id === 'overall' ? 'К справке' : issue.claim_id} · {issueKinds[issue.kind] ?? issue.kind}</strong><p>{issue.reason}</p>
        <References ids={issue.evidence_ids} onEvidence={onEvidence} />
      </li>)}</ul>}
    <TextList title="Альтернативные объяснения" items={review.alternative_explanations} />
    <TextList title="Каких сведений не хватает" items={review.missing_information} />
  </div>;
}

function Evidence({ packet, selected, open, onToggle }: { packet: EvidencePacket; selected: string | null; open: boolean; onToggle: (open: boolean) => void }) {
  const className = (id: string) => selected === id ? 'ai-source selected' : 'ai-source';
  return <details className="ai-sources" open={open} onToggle={event => onToggle(event.currentTarget.open)}>
    <summary>Исходные факты и ссылки</summary>
    <p className="ai-source-intro">Значения из локального расчёта, использованные для справки. Период: {packet.period.start} — {packet.period.end}.</p>
    <dl>{packet.facts.map(fact => <div key={fact.evidence_id} id={evidenceElementId(packet.gid, fact.evidence_id)} className={className(fact.evidence_id)} tabIndex={-1}>
      <dt><code>{fact.evidence_id}</code> {fact.label}</dt>
      <dd>{fact.value === null ? 'Не определено' : typeof fact.value === 'boolean' ? (fact.value ? 'Да' : 'Нет')
        : typeof fact.value === 'number' ? (fact.unit === 'KZT' ? formatMoney : formatNumber).format(fact.value) : fact.value}
        {fact.value !== null && fact.unit ? ` ${fact.unit}` : ''}</dd>
      <small>{fact.source}</small>
    </div>)}</dl>
    <h5>Контрагенты в пакете: {packet.returned_neighbors} из {packet.total_neighbors}</h5>
    {packet.truncated && <p className="ai-note">Пакет сокращён до значимых связей. Полные результаты доступны в CSV.</p>}
    {packet.neighbors.length === 0 && <p className="ai-note">Наблюдаемых связей нет.</p>}
    {packet.neighbors.map(link => <div key={link.evidence_id} id={evidenceElementId(packet.gid, link.evidence_id)} className={className(link.evidence_id)} tabIndex={-1}>
      <code>{link.evidence_id}</code><p className="ai-link-direction">{link.src} → {link.dst}</p>
      <p>{formatMoney.format(link.sum_kzt)} KZT · {link.n_tx.toLocaleString('ru-RU')} переводов</p><small>{link.source}</small>
    </div>)}
    <h5>Ограничения наблюдения</h5>
    {packet.caveats.map(caveat => <div key={caveat.evidence_id} id={evidenceElementId(packet.gid, caveat.evidence_id)} className={className(caveat.evidence_id)} tabIndex={-1}>
      <code>{caveat.evidence_id}</code><p>{caveat.text}</p><small>{caveat.source}</small>
    </div>)}
  </details>;
}

export default function AiPanel({ gid }: { gid: string }) {
  const [config, setConfig] = useState<AiConfig | null>(null);
  const [configError, setConfigError] = useState('');
  const [job, setJob] = useState<AiJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const [selectedEvidence, setSelectedEvidence] = useState<string | null>(null);
  const activeRequest = useRef<AbortController | null>(null);
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const openButtonRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const dialog = dialogRef.current;
    void fetchAiConfig(controller.signal).then(value => { if (!controller.signal.aborted) setConfig(value); })
      .catch(reason => { if (!controller.signal.aborted) setConfigError(reason instanceof Error ? reason.message : 'Статус AI-сервисов недоступен.'); });
    return () => { controller.abort(); activeRequest.current?.abort(); dialog?.close(); };
  }, [gid]);

  useEffect(() => {
    if (!selectedEvidence || !sourcesOpen) return;
    const element = document.getElementById(evidenceElementId(gid, selectedEvidence));
    element?.scrollIntoView({ block: 'nearest' });
    element?.focus({ preventScroll: true });
  }, [gid, selectedEvidence, sourcesOpen]);

  async function run(refresh = false) {
    if (activeRequest.current) return;
    const controller = new AbortController();
    activeRequest.current = controller;
    setBusy(true);
    setError('');
    try {
      let current = job && job.status !== 'completed' && !refresh
        ? await fetchAiJob(job.job_id, gid, controller.signal)
        : await startAiJob(gid, refresh, controller.signal);
      if (controller.signal.aborted) return;
      setJob(current);
      const deadline = Date.now() + 60_000;
      while (current.status !== 'completed') {
        if (Date.now() > deadline) throw new Error('Разбор ещё выполняется. Нажмите «Проверить результат» — новый запрос к моделям не нужен.');
        await delay(controller.signal);
        current = await fetchAiJob(current.job_id, gid, controller.signal);
        if (controller.signal.aborted) return;
        setJob(current);
      }
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : 'Не удалось выполнить AI-разбор.');
    } finally {
      if (!controller.signal.aborted) setBusy(false);
      if (activeRequest.current === controller) activeRequest.current = null;
    }
  }

  function showEvidence(id: string) {
    setSelectedEvidence(id);
    setSourcesOpen(true);
    // Clicking the same source a second time should still navigate to it.
    if (selectedEvidence === id && sourcesOpen) {
      const element = document.getElementById(evidenceElementId(gid, id));
      element?.scrollIntoView({ block: 'nearest' });
      element?.focus({ preventScroll: true });
    }
  }

  function openDialog() {
    if (!dialogRef.current?.open) dialogRef.current?.showModal();
    if (!job && !busy) void run();
  }

  const reviewerEnabled = job?.reviewer_enabled ?? config?.reviewer_enabled ?? false;
  const report = job && isReport(job.openai.result) ? job.openai.result : null;
  const review = reviewerEnabled && job && isReview(job.nvidia.result) ? job.nvidia.result : null;
  const missing = config ? [!config.openai.configured && 'OpenAI', config.reviewer_enabled && !config.nvidia.configured && 'NVIDIA'].filter(Boolean) : [];
  const title = reviewerEnabled ? 'Два взгляда на факты' : 'AI-разбор участника';
  const compactStatus = busy ? 'Разбор выполняется. Можно продолжить работу с графом.'
    : error ? 'Разбор требует внимания. Откройте подробности.'
    : job?.fallback ? 'Готова локальная справка по правилам Fusion.'
    : report ? 'Справка готова. Повторное открытие не вызывает API.'
    : config && !config.openai.configured ? 'Для AI нужен ключ OpenAI; локальная справка доступна.'
    : 'Гипотеза, основания и следующие проверки.';

  return <><section className="ai-panel ai-panel-compact" aria-labelledby={`ai-title-${gid}`}>
    <div className="ai-title"><h3 id={`ai-title-${gid}`}>{title}</h3><span>AI</span></div>
    <button ref={openButtonRef} type="button" className="primary ai-open-button" onClick={openDialog}>{job || busy ? 'Открыть AI-разбор' : 'AI-разбор'}</button>
    <p className="ai-compact-status" role="status">{compactStatus}</p>
  </section>
  <dialog ref={dialogRef} className="ai-dialog" aria-labelledby={`ai-dialog-title-${gid}`} aria-describedby={`ai-dialog-description-${gid}`}
    onClose={() => { if (openButtonRef.current?.isConnected) openButtonRef.current.focus({ preventScroll: true }); }}>
    <header className="ai-dialog-header">
      <div><h2 id={`ai-dialog-title-${gid}`}>{title}</h2><p>ID участника <strong>{gid}</strong></p></div>
      <button type="button" className="ai-dialog-close" onClick={() => dialogRef.current?.close()} aria-label="Закрыть AI-разбор">Закрыть <span aria-hidden="true">×</span></button>
    </header>
    <div className="ai-dialog-body">
    <p id={`ai-dialog-description-${gid}`} className="ai-intro">{reviewerEnabled ? 'OpenAI составляет справку. NVIDIA ищет неподтверждённые выводы и другие объяснения.' : 'OpenAI объясняет рассчитанные признаки и предлагает следующие проверки.'}</p>
    {missing.length > 0 && <p className="ai-config-note">Не настроен {missing.join(' / ')}. Добавьте ключ в backend/.env и перезапустите сервер. {reviewerEnabled ? 'Базовые расчёты работают; разбор без обоих API будет неполным.' : 'До подключения API доступна локальная справка по рассчитанным фактам.'}</p>}
    {configError && !job && <p className="ai-config-note">{configError}</p>}
    <div className="ai-actions">
      {(busy || !job || job.status !== 'completed' || error) && <button type="button" className="primary" disabled={busy} onClick={() => void run()}>
        {busy ? 'Разбор выполняется…' : job && job.status !== 'completed' ? 'Проверить результат' : 'Повторить AI-разбор'}
      </button>}
      {job && !busy && <button type="button" className="ai-refresh" onClick={() => void run(true)} title="Обойти кеш и заново выполнить AI-разбор">{reviewerEnabled ? 'Обновить · новые запросы' : 'Обновить · новый запрос'}</button>}
    </div>
    <p className="ai-progress" role="status">{busy ? report && reviewerEnabled ? 'Справка готова. Ожидаем рецензию NVIDIA…' : 'Готовим разбор выбранного участника…'
      : job?.status === 'completed' ? 'Разбор завершён. Результат указан ниже.' : reviewerEnabled ? 'Один участник · до двух запросов · повторный разбор использует кеш' : 'Один участник · один запрос · повторный разбор использует кеш'}</p>
    {error && <p className="ai-error" role="alert">{error}</p>}
    {job && <div className="ai-results">
      {job.warning && <p className="ai-warning">{job.warning}</p>}
      <section className="ai-stage">
        <StageHeader title="Справка OpenAI" stage={job.openai} />
        {report && <Report report={report} review={review} onEvidence={showEvidence} />}
        {!report && job.openai.status === 'running' && <p className="ai-note">Аналитик изучает пакет фактов…</p>}
      </section>
      {job.fallback && <section className="ai-fallback"><h4>Локальная справка (fallback)</h4><p className="ai-note">Составлена по правилам Fusion. {reviewerEnabled ? 'Это не ответ OpenAI или NVIDIA.' : 'Это не ответ OpenAI.'}</p>
        <Report report={job.fallback} review={null} onEvidence={showEvidence} /></section>}
      {reviewerEnabled && <section className="ai-stage">
        <StageHeader title="Замечания NVIDIA" stage={job.nvidia} />
        {review && <Review review={review} onEvidence={showEvidence} />}
        {job.nvidia.status === 'running' && <p className="ai-note">Рецензент проверяет справку по тем же фактам…</p>}
        {job.nvidia.status === 'pending' && <p className="ai-note">Ожидает валидную справку OpenAI.</p>}
        {job.nvidia.status === 'skipped' && <p className="ai-note">Рецензия не выполнялась; работа двух API не подтверждена.</p>}
        {job.nvidia.status === 'error' && <p className="ai-note">Рецензия недоступна. Справка OpenAI сохранена без проверки второй моделью.</p>}
      </section>}
      <div className="ai-observation"><h4>Границы этих данных</h4><ul>{job.evidence_packet.caveats.map(caveat => <li key={caveat.evidence_id}>{caveat.text} <References ids={[caveat.evidence_id]} onEvidence={showEvidence} /></li>)}</ul></div>
      <Evidence packet={job.evidence_packet} selected={selectedEvidence} open={sourcesOpen} onToggle={setSourcesOpen} />
    </div>}
    <p className="ai-disclaimer">AI помогает интерпретировать данные; выводы требуют проверки аналитиком. Роли и скоры остаются результатом локального расчёта.</p>
    </div>
  </dialog></>;
}
