/**
 * api.ts — talks to the existing FastAPI backend. No new endpoints needed.
 *
 * Auth mirrors the web client exactly: a read passphrase on every request, and
 * a trade PIN required additionally for anything non-GET. This app only ever
 * sends the read passphrase — it is a monitor, and a phone should not be able
 * to move money. The server enforces that independently (deny-by-method), so
 * this is a second lock rather than the only one.
 */
import AsyncStorage from '@react-native-async-storage/async-storage';

const KEY_PASS = 'raanu.pass';
const KEY_HOST = 'raanu.host';

export const DEFAULT_HOST = 'https://raanu.up.railway.app';

let _pass: string | null = null;
let _host: string | null = null;

export async function loadCreds() {
  _pass = await AsyncStorage.getItem(KEY_PASS);
  _host = (await AsyncStorage.getItem(KEY_HOST)) || DEFAULT_HOST;
  return { pass: _pass, host: _host };
}

export async function setPass(p: string | null) {
  _pass = p;
  if (p) await AsyncStorage.setItem(KEY_PASS, p);
  else await AsyncStorage.removeItem(KEY_PASS);
}

export async function setHost(h: string) {
  _host = h || DEFAULT_HOST;
  await AsyncStorage.setItem(KEY_HOST, _host);
}

export const host = () => _host || DEFAULT_HOST;
export const hasPass = () => !!_pass;

export class Unauthorized extends Error {}

/**
 * Every read goes through here, so a 401 surfaces as one recognisable error the
 * UI can turn into the unlock screen — rather than each screen inventing its
 * own empty state and leaving the user guessing why nothing loaded.
 */
export async function get<T = any>(path: string, timeoutMs = 20000): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(host() + path, {
      headers: _pass ? { Authorization: `Bearer ${_pass}` } : {},
      signal: ctrl.signal,
    });
    if (r.status === 401) throw new Unauthorized('bad passphrase');
    if (!r.ok) throw new Error(`${path} → ${r.status}`);
    return (await r.json()) as T;
  } finally {
    clearTimeout(timer);
  }
}

/** Push registration and its self-test are the only non-GETs this app makes;
 *  the server exempts them from the trade PIN because they move no money. */
export async function post<T = any>(path: string, body?: any): Promise<T> {
  const r = await fetch(host() + path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(_pass ? { Authorization: `Bearer ${_pass}` } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401) throw new Unauthorized('bad passphrase');
  return (await r.json().catch(() => ({}))) as T;
}

/**
 * Actions that move money. The trade PIN is passed in per call and NEVER
 * stored — not in AsyncStorage, not in a module variable that outlives the
 * request. A borrowed or stolen phone should not be able to sell a position,
 * which is the whole reason the two secrets are separate.
 */
export async function tradeAction<T = any>(path: string, pin: string, body?: any): Promise<{
  ok: boolean; status: number; data: T | null;
}> {
  const r = await fetch(host() + path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(_pass ? { Authorization: `Bearer ${_pass}` } : {}),
      'X-Trade-Token': pin,
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => null);
  return { ok: r.ok, status: r.status, data };
}

// ---- endpoint shapes actually used by the screens ----
export const api = {
  health: () => get('/api/health'),
  cash: () => get('/api/account/cash'),
  portfolio: () => get('/api/portfolio'),
  orders: (limit = 200) => get(`/api/history/orders?limit=${limit}`),
  autoStatus: () => get('/api/auto/status'),
  compare: () => get('/api/strategy/compare'),
  exitConfig: () => get('/api/exit-config'),
  outcomes: (limit = 40) => get(`/api/picks/outcomes?limit=${limit}`),
  pushKey: () => get('/api/push/key'),
  pushSubscribe: (sub: any) => post('/api/push/subscribe', sub),
  pushTest: () => post('/api/push/test'),
};
