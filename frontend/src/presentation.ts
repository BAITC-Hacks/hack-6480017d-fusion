import type { Role } from './api';

export const roleInfo: Record<Role, { label: string; color: string }> = {
  consolidator: { label: 'Консолидатор', color: '#62c7ff' },
  transit: { label: 'Транзитный', color: '#53d58a' },
  distributor: { label: 'Распределитель', color: '#ffc56e' },
  terminal: { label: 'Конечный', color: '#ff7a7a' },
  coordinator: { label: 'Координатор', color: '#b79aff' },
  peripheral: { label: 'Периферийный', color: '#74889b' },
};
export const count = (value: number) => new Intl.NumberFormat('ru-RU').format(value);
export const money = (value: number) => `${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 }).format(value)} ₸`;
export const score = (value: number) => value.toFixed(3);
export const shortGid = (gid: string) => gid.length > 10 ? `…${gid.slice(-7)}` : gid;
export const clusterColor = (id: number) => `hsl(${(id * 137.508 + 160) % 360}, 60%, 66%)`;
