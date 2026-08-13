/**
 * format.ts — number formatting, ported verbatim from the web dashboard.
 *
 * These are shared on purpose. If the app and the dashboard round or sign
 * differently, the same account reads as two different accounts, and the first
 * assumption is that one of them is broken.
 */

export const money = (n: number) =>
  '$' + Number(n || 0).toLocaleString('en-US', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });

export const signed = (n: number) =>
  (n >= 0 ? '+' : '-') + '$' +
  Math.abs(Number(n || 0)).toLocaleString('en-US', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });

export const pct = (n: number) => (n >= 0 ? '+' : '') + Number(n || 0).toFixed(2) + '%';

/** Zero counts as neutral-positive, matching the web's cls(). */
export const isUp = (n: number) => Number(n || 0) >= 0;

/** Strategy tag from any record that might carry one. */
export const sof = (o: any): string => {
  const s = String(o?.strategy || '').toLowerCase();
  return ['s1', 's2', 's3'].includes(s) ? s : '';
};

export const shortTime = (iso?: string | null) => {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(+d) ? '' : d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
};

export const shortDate = (iso?: string | null) => {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(+d) ? '' : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
};

/**
 * Next scheduled scan, in ET — the clock the bot's scheduler actually runs on.
 * Slots are 09:35 and 11:00 ET on weekdays; see _ET_SLOTS in server.py.
 */
export function nextScan(): { label: string; inText: string } | null {
  const SLOTS: [number, number][] = [[9, 35], [11, 0]];
  const nowET = new Date(new Date().toLocaleString('en-US', { timeZone: 'America/New_York' }));
  for (let d = 0; d < 7; d++) {
    for (const [h, m] of SLOTS) {
      const t = new Date(nowET);
      t.setDate(t.getDate() + d);
      t.setHours(h, m, 0, 0);
      if (t > nowET && t.getDay() >= 1 && t.getDay() <= 5) {
        const mins = Math.round((+t - +nowET) / 60000);
        const inText =
          mins < 60 ? `${mins} min`
          : mins < 1440 ? `${Math.floor(mins / 60)}h ${mins % 60}m`
          : `${Math.round(mins / 1440)}d`;
        return {
          label: `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')} ET`,
          inText,
        };
      }
    }
  }
  return null;
}
