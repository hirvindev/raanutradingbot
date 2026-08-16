/**
 * push.ts — native notification registration.
 *
 * Android push always goes through FCM. Expo's push service is a hosted wrapper
 * over it, so either route needs a Firebase project — there is no account-free
 * path to a notification that arrives while the app is closed. The one thing
 * this cannot do for you is create that project.
 *
 * What arrives: BUY, EXIT and ERROR. Scans and "no signal today" deliberately
 * do not push. A channel that fires on everything gets dismissed by reflex, and
 * then the stop-out gets dismissed with it. Telegram remains the full record.
 */
import * as Notifications from 'expo-notifications';
import * as Device from 'expo-device';
import { Platform } from 'react-native';
import { post } from './api';

// Show notifications even when the app is foregrounded — otherwise a fill that
// happens while you are looking at the app is silently swallowed.
Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowBanner: true,
    shouldShowList: true,
    shouldPlaySound: true,
    shouldSetBadge: false,
  }),
});

export type PushState = 'on' | 'off' | 'blocked' | 'unsupported' | 'no-fcm';

export async function pushState(): Promise<PushState> {
  if (!Device.isDevice) return 'unsupported';        // emulators cannot receive
  const { status } = await Notifications.getPermissionsAsync();
  if (status === 'denied') return 'blocked';
  return status === 'granted' ? 'on' : 'off';
}

/**
 * Ask, register, and tell the server. Returns a human-readable outcome rather
 * than a boolean: "it didn't work" without a reason is exactly what made the
 * web push attempt feel broken when it was merely unconfigured.
 */
export async function enablePush(): Promise<{ ok: boolean; msg: string }> {
  if (!Device.isDevice) {
    return { ok: false, msg: 'Notifications need a real device, not an emulator.' };
  }

  if (Platform.OS === 'android') {
    // Android 8+ ignores notifications that have no channel.
    await Notifications.setNotificationChannelAsync('trades', {
      name: 'Trades and exits',
      importance: Notifications.AndroidImportance.HIGH,
      vibrationPattern: [0, 90, 50, 90],
      lightColor: '#6941c6',
    });
  }

  let { status } = await Notifications.getPermissionsAsync();
  if (status !== 'granted') ({ status } = await Notifications.requestPermissionsAsync());
  if (status !== 'granted') return { ok: false, msg: 'Permission denied.' };

  let token: string;
  try {
    // Needs FCM credentials in the build. Without google-services.json this
    // throws, and the message says so instead of failing silently.
    const t = await Notifications.getDevicePushTokenAsync();
    token = String(t.data);
  } catch (e: any) {
    // Report what actually failed. This previously claimed "no Firebase config"
    // for ANY exception — a cause it had not verified — which sent days of
    // debugging at the build while the real fault was the server's credentials.
    return { ok: false, msg: `Could not get a device token: ${e?.message || e}` };
  }

  try {
    const r: any = await post('/api/push/native/register', {
      token, platform: Platform.OS,
    });
    return r?.ok
      ? { ok: true, msg: `Registered. ${r.devices} device(s) will be notified.` }
      : { ok: false, msg: r?.error || 'Server rejected the token.' };
  } catch (e: any) {
    return { ok: false, msg: 'Could not reach the server.' };
  }
}

export async function sendTest(): Promise<string> {
  try {
    const r: any = await post('/api/push/test');
    if (r?.sent) return `Test sent to ${r.sent} device(s).`;
    return r?.skipped || r?.error || 'Nothing was sent.';
  } catch {
    return 'Could not reach the server.';
  }
}
