// gid/src/dst come from the API as decimal strings. Never parse them as Number.
export type Gid = string;

export interface Summary {
  nodes: number;
  edges: number;
  transactions: number;
  seed_nodes: number;
  period: { start: string; end: string };
}

async function fetchJson(path: string, signal: AbortSignal): Promise<unknown> {
  const controller = new AbortController();
  let timedOut = false;
  const cancel = () => controller.abort();
  signal.addEventListener('abort', cancel, { once: true });
  if (signal.aborted) controller.abort();
  const timeout = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, 10_000);

  try {
    const response = await fetch(path, { signal: controller.signal });
    if (!response.ok) throw new Error(`Ошибка сервера: HTTP ${response.status}.`);
    return await response.json();
  } catch (error) {
    if (timedOut) throw new Error('Сервер не ответил за 10 секунд.');
    if (error instanceof TypeError) throw new Error('Не удалось связаться с локальным сервером.');
    throw error;
  } finally {
    window.clearTimeout(timeout);
    signal.removeEventListener('abort', cancel);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isCount(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}

function isDate(value: unknown): value is string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value;
}

export async function fetchHealth(signal: AbortSignal): Promise<void> {
  const data = await fetchJson('/api/health', signal);
  if (!isRecord(data) || data.status !== 'ok' || data.service !== 'Fusion') {
    throw new Error('Сервер вернул неожиданный статус.');
  }
}

export async function fetchSummary(signal: AbortSignal): Promise<Summary> {
  const data = await fetchJson('/api/summary', signal);
  if (
    !isRecord(data) ||
    !isCount(data.nodes) || !isCount(data.edges) ||
    !isCount(data.transactions) || !isCount(data.seed_nodes) ||
    !isRecord(data.period) ||
    !isDate(data.period.start) || !isDate(data.period.end) ||
    data.period.start > data.period.end
  ) {
    throw new Error('Сервер вернул сводку в неожиданном формате.');
  }
  return {
    nodes: data.nodes,
    edges: data.edges,
    transactions: data.transactions,
    seed_nodes: data.seed_nodes,
    period: { start: data.period.start, end: data.period.end },
  };
}
