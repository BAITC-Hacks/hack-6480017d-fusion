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
    if (!response.ok) {
      if (response.status === 404) throw new Error('Участник или кластер не найден в выгрузке.');
      if (response.status === 422) throw new Error('Проверьте ID: нужно целое число без пробелов.');
      if (response.status === 503) throw new Error('Расчёт недоступен. Проверьте данные и перезапустите сервер.');
      if (response.status >= 500) throw new Error(`Нет ответа от сервера (HTTP ${response.status}). Убедитесь, что локальный сервер запущен, и повторите запрос.`);
      throw new Error(`Ошибка сервера: HTTP ${response.status}.`);
    }
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

export type Role = 'consolidator' | 'transit' | 'distributor' | 'terminal' | 'coordinator' | 'peripheral';
export interface TopNode { rank: number; gid: Gid; role: Role; priority_score: number; why: string }
export interface Cluster {
  cluster_id: number; n_nodes: number; n_seed: number; sum_kzt_internal: number;
  top_gids: Gid[]; hypothesis: string;
}
export interface Analysis extends Summary {
  clusters_count: number; total_kzt: number; role_counts: Record<Role, number>;
  top_nodes: TopNode[]; clusters: Cluster[];
}
export interface GraphNode {
  gid: Gid; role: Role; priority_score: number; cluster_id: number; is_seed: boolean;
  depth: number; truncated_by_depth: boolean; is_isolated: boolean;
}
export interface GraphData {
  nodes: GraphNode[];
  edges: { src: Gid; dst: Gid; sum_kzt: number; n_tx: number }[];
  total_nodes: number; returned_nodes: number; total_edges: number; returned_edges: number;
  truncated: boolean; scope: string;
}
export interface NodeDetail extends GraphNode {
  role_score: number; evidence: string; in_deg: number; out_deg: number;
  in_kzt: number; out_kzt: number; in_tx: number; out_tx: number; pagerank: number;
  seed_reach: number; pass_through: number | null; ratio_usable: boolean; caveats: string[];
}
export type GraphScope = { gid: Gid } | { cluster_id: number };

const isGid = (value: unknown): value is Gid => typeof value === 'string' && /^-?(0|[1-9]\d*)$/.test(value);
const isFiniteNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const roles: Role[] = ['consolidator', 'transit', 'distributor', 'terminal', 'coordinator', 'peripheral'];
const isRole = (value: unknown): value is Role => typeof value === 'string' && roles.includes(value as Role);
const isScore = (value: unknown): value is number => isFiniteNumber(value) && value >= 0 && value <= 1;
const isGraphNode = (n: unknown): boolean => isRecord(n) && isGid(n.gid) && isRole(n.role) &&
  isCount(n.cluster_id) && isCount(n.depth) && isScore(n.priority_score) &&
  typeof n.is_seed === 'boolean' && typeof n.truncated_by_depth === 'boolean' && typeof n.is_isolated === 'boolean';

export async function fetchAnalysis(signal: AbortSignal): Promise<Analysis> {
  const data = await fetchJson('/api/analysis', signal);
  if (!isRecord(data) || !isCount(data.nodes) || !isCount(data.edges) ||
    !isCount(data.transactions) || !isCount(data.seed_nodes) || !isCount(data.clusters_count) ||
    !isFiniteNumber(data.total_kzt) || !isRecord(data.period) || !isDate(data.period.start) || !isDate(data.period.end) ||
    !isRecord(data.role_counts) || !Array.isArray(data.top_nodes) || !Array.isArray(data.clusters) ||
    !data.top_nodes.every(n => isRecord(n) && isGid(n.gid) && isRole(n.role) && isCount(n.rank) && isFiniteNumber(n.priority_score) && typeof n.why === 'string') ||
    !data.clusters.every(c => isRecord(c) && isCount(c.cluster_id) && isCount(c.n_nodes) && isCount(c.n_seed) &&
      isFiniteNumber(c.sum_kzt_internal) && typeof c.hypothesis === 'string' && Array.isArray(c.top_gids) && c.top_gids.every(isGid))) {
    throw new Error('Неожиданный формат результатов расчёта.');
  }
  return data as unknown as Analysis;
}

export async function fetchGraph(scope: GraphScope, signal: AbortSignal): Promise<GraphData> {
  const query = 'gid' in scope ? `gid=${encodeURIComponent(scope.gid)}` : `cluster_id=${scope.cluster_id}`;
  const data = await fetchJson(`/api/graph?${query}&limit=250`, signal);
  if (!isRecord(data) || !Array.isArray(data.nodes) || !Array.isArray(data.edges) ||
    !isCount(data.total_nodes) || !isCount(data.returned_nodes) || !isCount(data.total_edges) || !isCount(data.returned_edges) ||
    typeof data.scope !== 'string' || typeof data.truncated !== 'boolean' ||
    !data.nodes.every(isGraphNode) ||
    !data.edges.every(e => isRecord(e) && isGid(e.src) && isGid(e.dst) && isFiniteNumber(e.sum_kzt) && isCount(e.n_tx))) {
    throw new Error('Неожиданный формат графа.');
  }
  const graph = data as unknown as GraphData;
  const ids = new Set(graph.nodes.map(n => n.gid));
  if (ids.size !== graph.nodes.length || graph.returned_nodes !== graph.nodes.length ||
    graph.returned_edges !== graph.edges.length || graph.total_nodes < graph.returned_nodes ||
    graph.total_edges < graph.returned_edges || !graph.edges.every(e => ids.has(e.src) && ids.has(e.dst))) {
    throw new Error('Нарушена целостность графа: участники и связи не согласованы.');
  }
  return graph;
}

export async function fetchNode(gid: Gid, signal: AbortSignal): Promise<NodeDetail> {
  const data = await fetchJson(`/api/nodes/${encodeURIComponent(gid)}`, signal);
  if (!isRecord(data) || !isGraphNode(data) || data.gid !== gid ||
    !isScore(data.role_score) || typeof data.ratio_usable !== 'boolean' || typeof data.evidence !== 'string' ||
    !['in_deg', 'out_deg', 'in_kzt', 'out_kzt', 'in_tx', 'out_tx', 'pagerank', 'seed_reach', 'depth'].every(k => isFiniteNumber(data[k])) ||
    !(data.pass_through === null || isFiniteNumber(data.pass_through)) ||
    !Array.isArray(data.caveats) || !data.caveats.every(c => typeof c === 'string')) {
    throw new Error('Неожиданный формат карточки участника.');
  }
  return data as unknown as NodeDetail;
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
