export type EvidenceFact = {
  evidence_id: string; label: string; value: number | string | boolean | null; unit: string; source: string;
};
export type EvidenceNeighbor = {
  evidence_id: string; src: string; dst: string; sum_kzt: number; n_tx: number; source: string;
};
export type EvidencePacket = {
  version: string; gid: string; data_fingerprint: string; period: { start: string; end: string }; role: string;
  facts: EvidenceFact[]; neighbors: EvidenceNeighbor[]; total_neighbors: number; returned_neighbors: number;
  truncated: boolean; caveats: { evidence_id: string; text: string; source: string }[];
};
export type AiReport = {
  hypothesis: string; claims: { claim_id: string; text: string; evidence_ids: string[] }[];
  limitations: string[]; next_checks: string[];
};
export type AiReview = {
  issues: { claim_id: string; kind: string; reason: string; evidence_ids: string[] }[];
  alternative_explanations: string[]; missing_information: string[];
};
export type AiStage = {
  status: 'queued' | 'pending' | 'running' | 'success' | 'cached' | 'error' | 'skipped'; model: string | null;
  elapsed_ms: number | null; usage: { input_tokens: number | null; output_tokens: number | null; total_tokens: number | null } | null;
  error: { code: string; message: string } | null; result: AiReport | AiReview | null; cached_at: string | null;
};
export type AiJob = {
  job_id: string; gid: string; status: 'queued' | 'running' | 'completed'; created_at: string; completed_at: string | null;
  reviewer_enabled: boolean;
  evidence_packet: EvidencePacket; openai: AiStage; nvidia: AiStage; fallback: AiReport | null; warning: string | null;
};
export type AiConfig = {
  openai: { configured: boolean; model: string }; nvidia: { configured: boolean; model: string }; timeout_seconds: number;
  reviewer_enabled: boolean;
};

const record = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === 'string';
const nullableText = (value: unknown) => value === null || text(value);
const number = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const tokenCount = (value: unknown) => value === null || (number(value) && Number.isSafeInteger(value) && value >= 0);
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every(text);
const gid = (value: unknown): value is string => text(value) && /^-?(0|[1-9]\d*)$/.test(value);

export function isReport(value: unknown): value is AiReport {
  return record(value) && text(value.hypothesis) && Array.isArray(value.claims)
    && value.claims.every(item => record(item) && text(item.claim_id) && text(item.text) && strings(item.evidence_ids))
    && strings(value.limitations) && strings(value.next_checks);
}

export function isReview(value: unknown): value is AiReview {
  return record(value) && Array.isArray(value.issues)
    && value.issues.every(item => record(item) && text(item.claim_id) && text(item.kind) && text(item.reason) && strings(item.evidence_ids))
    && strings(value.alternative_explanations) && strings(value.missing_information);
}

function isStage(value: unknown): value is AiStage {
  return record(value) && ['queued', 'pending', 'running', 'success', 'cached', 'error', 'skipped'].includes(String(value.status))
    && nullableText(value.model) && (value.elapsed_ms === null || number(value.elapsed_ms))
    && (value.usage === null || (record(value.usage) && tokenCount(value.usage.input_tokens) && tokenCount(value.usage.output_tokens) && tokenCount(value.usage.total_tokens)))
    && (value.error === null || (record(value.error) && text(value.error.code) && text(value.error.message)))
    && (value.result === null || isReport(value.result) || isReview(value.result)) && nullableText(value.cached_at);
}

function isPacket(value: unknown): value is EvidencePacket {
  return record(value) && text(value.version) && gid(value.gid) && text(value.data_fingerprint) && text(value.role)
    && record(value.period) && text(value.period.start) && text(value.period.end)
    && Array.isArray(value.facts) && value.facts.every(item => record(item) && text(item.evidence_id) && text(item.label)
      && (item.value === null || number(item.value) || text(item.value) || typeof item.value === 'boolean') && text(item.unit) && text(item.source))
    && Array.isArray(value.neighbors) && value.neighbors.every(item => record(item) && text(item.evidence_id)
      && gid(item.src) && gid(item.dst) && number(item.sum_kzt) && number(item.n_tx) && text(item.source))
    && number(value.total_neighbors) && number(value.returned_neighbors) && typeof value.truncated === 'boolean'
    && Array.isArray(value.caveats) && value.caveats.every(item => record(item) && text(item.evidence_id) && text(item.text) && text(item.source));
}

function parseJob(value: unknown): AiJob {
  if (!record(value) || !text(value.job_id) || !gid(value.gid) || !['queued', 'running', 'completed'].includes(String(value.status))
    || typeof value.reviewer_enabled !== 'boolean' || !text(value.created_at) || !nullableText(value.completed_at) || !isPacket(value.evidence_packet)
    || !isStage(value.openai) || !isStage(value.nvidia) || !(value.fallback === null || isReport(value.fallback)) || !nullableText(value.warning)) {
    throw new Error('Сервер вернул неполный AI-разбор. Повторите чтение результата.');
  }
  if (value.evidence_packet.gid !== value.gid) throw new Error('ID справки не совпадает с пакетом фактов.');
  if (['success', 'cached'].includes(value.openai.status) && !isReport(value.openai.result)) {
    throw new Error('В ответе OpenAI отсутствует справка ожидаемого формата.');
  }
  if (['success', 'cached'].includes(value.nvidia.status) && !isReview(value.nvidia.result)) {
    throw new Error('В ответе NVIDIA отсутствует рецензия ожидаемого формата.');
  }
  return value as AiJob;
}

async function request(path: string, signal: AbortSignal, body?: unknown): Promise<unknown> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener('abort', abort, { once: true });
  if (signal.aborted) controller.abort();
  const timer = window.setTimeout(abort, 12_000);
  try {
    const response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST', signal: controller.signal,
      ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
    });
    if (!response.ok) {
      if (response.status === 404) throw new Error('AI-разбор пока недоступен или сервер был перезапущен. Запустите разбор ещё раз.');
      if (response.status === 429) throw new Error('Другой разбор уже выполняется. Дождитесь его завершения.');
      throw new Error(`Не удалось получить AI-разбор (HTTP ${response.status}). Расчёт и граф продолжают работать.`);
    }
    return await response.json();
  } catch (error) {
    if (signal.aborted) throw error;
    if (controller.signal.aborted) throw new Error('Сервер не ответил вовремя. Проверьте результат ещё раз.');
    if (error instanceof TypeError) throw new Error('Нет связи с локальным сервером. Восстановите соединение и проверьте результат.');
    throw error;
  } finally {
    window.clearTimeout(timer);
    signal.removeEventListener('abort', abort);
  }
}

export async function fetchAiConfig(signal: AbortSignal): Promise<AiConfig> {
  const value = await request('/api/ai/status', signal);
  if (!record(value) || !record(value.openai) || !record(value.nvidia) || typeof value.openai.configured !== 'boolean'
    || typeof value.nvidia.configured !== 'boolean' || typeof value.reviewer_enabled !== 'boolean'
    || !text(value.openai.model) || !text(value.nvidia.model) || !number(value.timeout_seconds)) {
    throw new Error('Статус AI-сервисов временно недоступен.');
  }
  return value as AiConfig;
}

export async function startAiJob(id: string, refresh: boolean, signal: AbortSignal): Promise<AiJob> {
  const value = parseJob(await request('/api/ai/analyses', signal, { gid: id, refresh }));
  if (value.gid !== id) throw new Error('Получен разбор другого участника.');
  return value;
}

export async function fetchAiJob(jobId: string, id: string, signal: AbortSignal): Promise<AiJob> {
  const value = parseJob(await request(`/api/ai/analyses/${encodeURIComponent(jobId)}`, signal));
  if (value.gid !== id || value.job_id !== jobId) throw new Error('Получен результат другого разбора.');
  return value;
}
