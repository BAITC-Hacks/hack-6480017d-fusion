import type { Role } from './api';

export const roleInfo: Record<Role, { label: string; color: string }> = {
  consolidator: { label: 'Консолидатор', color: '#b87127' },
  transit: { label: 'Транзитный', color: '#2b879b' },
  distributor: { label: 'Распределитель', color: '#5875c1' },
  terminal: { label: 'Конечный', color: '#967259' },
  coordinator: { label: 'Координатор', color: '#127764' },
  peripheral: { label: 'Периферийный', color: '#97a3b0' },
};
export const count = (value: number) => new Intl.NumberFormat('ru-RU').format(value);
export const money = (value: number) => `${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 }).format(value)} ₸`;
export const score = (value: number) => value.toFixed(3);
export const shortGid = (gid: string) => gid.length > 10 ? `…${gid.slice(-7)}` : gid;
export const clusterColor = (id: number) => `hsl(${(id * 137.508 + 160) % 360}, 45%, 46%)`;
