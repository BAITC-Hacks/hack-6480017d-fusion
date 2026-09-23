export interface DatasetInfo {
  id: string;
  name: string;
  is_default: boolean;
  nodes: number;
  edges: number;
  transactions: number;
  period: { start: string; end: string };
  loaded_at: string;
}

export const DATASET_MAX_BYTES = 50 * 1024 * 1024;
const expectedNames = ['edges.parquet', 'nodes.parquet', 'transactions.parquet'];
const record = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value);
const count = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;

// Only inspect file metadata here. int64 identifiers are read and validated on the server.
export function validateDatasetFiles(files: readonly Pick<File, 'name' | 'size'>[]): string | null {
  if (files.length === 0) return 'Выберите один ZIP или все три файла Parquet.';
  if (files.some(file => !/\.(parquet|zip)$/i.test(file.name))) return 'Сейчас поддерживаются только ZIP или 3 Parquet по схеме кейса. CSV и Excel не поддерживаются.';
  if (files.some(file => file.size === 0)) return 'Один из выбранных файлов пуст. Выберите исходные файлы данных.';
  if (files.reduce((total, file) => total + file.size, 0) > DATASET_MAX_BYTES) return 'Общий размер выбранных файлов превышает 50 МиБ. Для ZIP учитывается размер архива.';
  if (files.length === 1 && /\.zip$/i.test(files[0].name)) return null;
  if (files.length !== 3 || files.some(file => !/\.parquet$/i.test(file.name))) return 'Выберите один ZIP либо одновременно nodes.parquet, edges.parquet и transactions.parquet.';
  if (new Set(files.map(file => file.name)).size !== 3) return 'Выбраны файлы с повторяющимися именами. Нужен один файл каждого типа.';
  if (!files.every(file => expectedNames.includes(file.name))) return 'Имена трёх файлов должны быть точно: nodes.parquet, edges.parquet, transactions.parquet.';
  return null;
}

function parseDataset(value: unknown): DatasetInfo {
  if (!record(value) || typeof value.id !== 'string' || typeof value.name !== 'string' || typeof value.is_default !== 'boolean'
    || !count(value.nodes) || !count(value.edges) || !count(value.transactions) || !record(value.period)
    || typeof value.period.start !== 'string' || typeof value.period.end !== 'string' || typeof value.loaded_at !== 'string') {
    throw new Error('Не удалось прочитать сведения о наборе. Обновите статус, чтобы проверить текущие данные.');
  }
  return value as unknown as DatasetInfo;
}

async function requestDataset(path: string, init: RequestInit = {}): Promise<DatasetInfo> {
  let response: Response;
  try {
    response = await fetch(path, { ...init, cache: 'no-store' });
  } catch (error) {
    if (init.signal?.aborted) throw error;
    throw new Error(init.method === 'POST'
      ? 'Связь с сервером прервалась. Обработка могла продолжиться: обновите статус перед повторной загрузкой.'
      : 'Нет связи с локальным сервером. Текущие данные пока недоступны.');
  }
  const value: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 404 && (!record(value) || value.detail === 'Not Found')) throw new Error('Загрузка данных пока недоступна на сервере. Обновите статус после его запуска.');
    if (record(value) && typeof value.detail === 'string') throw new Error(value.detail);
    if (record(value) && record(value.detail) && typeof value.detail.message === 'string') throw new Error(value.detail.message);
    if (response.status === 413) throw new Error('Размер загрузки превышает лимит сервера: не более 50 МиБ.');
    if (response.status === 404) throw new Error('Загрузка данных пока недоступна на сервере. Обновите статус после его запуска.');
    if (response.status === 409) throw new Error('Другой набор уже обрабатывается. Дождитесь завершения и обновите статус.');
    throw new Error(`Не удалось обработать данные (HTTP ${response.status}). Проверьте файлы и повторите попытку.`);
  }
  return parseDataset(value);
}

export function fetchCurrentDataset(signal: AbortSignal): Promise<DatasetInfo> {
  return requestDataset('/api/datasets/current', { signal });
}

export function uploadDataset(files: readonly File[]): Promise<DatasetInfo> {
  const issue = validateDatasetFiles(files);
  if (issue) return Promise.reject(new Error(issue));
  const body = new FormData();
  files.forEach(file => body.append('files', file));
  // Do not abort a mutation when its dialog closes: the server may already be applying it.
  return requestDataset('/api/datasets', { method: 'POST', body });
}

export function resetDataset(): Promise<DatasetInfo> {
  return requestDataset('/api/datasets/reset', { method: 'POST' });
}
