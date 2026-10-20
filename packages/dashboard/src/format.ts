/** metres → millimetres, one decimal */
export const mm = (m: number | undefined | null) => (typeof m === 'number' && Number.isFinite(m) ? (m * 1000).toFixed(1) : '—');

/** radians → degrees, one decimal */
export const deg = (rad: number | undefined | null) =>
  typeof rad === 'number' && Number.isFinite(rad) ? ((rad * 180) / Math.PI).toFixed(1) : '—';

export const px = (p: [number, number] | undefined | null) => (p ? `${Math.round(p[0])}, ${Math.round(p[1])}` : '—');

export function when(v: unknown): string {
  if (v == null || v === '') return '—';
  const d = typeof v === 'number' ? new Date(v < 1e12 ? v * 1000 : v) : new Date(String(v));
  if (Number.isNaN(d.getTime())) return String(v);
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}
