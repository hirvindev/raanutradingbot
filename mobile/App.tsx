/**
 * App.tsx — shell: unlock gate, theme, tabs, and one shared data load.
 *
 * All five screens read from a single fetch cycle rather than each polling
 * independently. On the web dashboard several panels fetching separately meant
 * one stale passphrase produced eight simultaneous 401s, which tripped the
 * server's brute-force lockout and locked the owner out of their own account.
 * One loader, one failure, one recovery path.
 */
import React, { useCallback, useEffect, useState } from 'react';
import {
  View, Text, TextInput, Pressable, ActivityIndicator, useColorScheme,
  StatusBar, SafeAreaView, Platform, Image,
} from 'react-native';
import { NavigationContainer, DefaultTheme, DarkTheme,
         createNavigationContainerRef } from '@react-navigation/native';
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import { SafeAreaProvider, useSafeAreaInsets } from 'react-native-safe-area-context';
import Svg, { Path, Circle } from 'react-native-svg';
import * as Notifications from 'expo-notifications';

import { api, loadCreds, setPass, hasPass, Unauthorized } from './src/api';
import { light, dark, type as T } from './src/theme';
import { ThemeCtx } from './src/ui';
import Dashboard from './src/screens/Dashboard';
import Orders from './src/screens/Orders';
import Signals from './src/screens/Signals';
import Strategy from './src/screens/Strategy';
import Logs from './src/screens/Logs';
import Alerts from './src/screens/Alerts';

const Tab = createBottomTabNavigator();

/**
 * Tapping a notification opens the app and Android dismisses the notification,
 * so the signal being read was simply gone — the alert carried the entry, the
 * stop and the reasoning, and it was the only copy.
 *
 * A tap now lands on Alerts, which holds the exact text of every notification
 * from the last 48 hours. Fighting the dismissal is the wrong fix; keeping a
 * copy is the right one.
 */
export const navRef = createNavigationContainerRef();

function goToAlerts() {
  if (navRef.isReady()) navRef.navigate('Alerts' as never);
}

const icons: Record<string, (c: string) => React.ReactNode> = {
  Home: c => <Svg width={22} height={22} viewBox="0 0 24 24" fill="none" stroke={c} strokeWidth={1.8}
    strokeLinecap="round" strokeLinejoin="round"><Path d="M3 12h4l3 8 4-16 3 8h4" /></Svg>,
  Orders: c => <Svg width={22} height={22} viewBox="0 0 24 24" fill="none" stroke={c} strokeWidth={1.8}
    strokeLinecap="round"><Path d="M4 6h16M4 12h16M4 18h10" /></Svg>,
  Signals: c => <Svg width={22} height={22} viewBox="0 0 24 24" fill="none" stroke={c} strokeWidth={1.8}
    strokeLinecap="round"><Path d="M4 18V9M10 18V5M16 18v-6M22 18v-9" /></Svg>,
  Strategy: c => <Svg width={22} height={22} viewBox="0 0 24 24" fill="none" stroke={c} strokeWidth={1.8}
    ><Circle cx={12} cy={12} r={8} /><Circle cx={12} cy={12} r={3} /></Svg>,
  Logs: c => <Svg width={22} height={22} viewBox="0 0 24 24" fill="none" stroke={c} strokeWidth={1.8}
    strokeLinecap="round" strokeLinejoin="round"><Path d="M5 4h14v16H5z" /><Path d="M8 9h8M8 13h8M8 17h5" /></Svg>,
};

export default function App() {
  // Light only, by preference. The dark palette is kept in theme.ts and is a
  // one-line change away, but following the system meant the app flipped to
  // dark on a phone set to dark and that is not what is wanted here.
  useColorScheme();          // referenced so the import stays meaningful
  const t = light;

  const [booted, setBooted] = useState(false);
  const [locked, setLocked] = useState(false);
  const [entry, setEntry] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [data, setData] = useState<any>({});

  const load = useCallback(async () => {
    try {
      // Settled, not all-or-nothing: one slow endpoint should not blank the
      // whole app, and /api/picks/outcomes is the newest and least proven.
      const [health, cash, portfolio, auto, compare, orders, exit, outcomes,
             alerts] = await Promise.all([
          api.health().catch(() => null),
          api.cash(),
          api.portfolio().catch(() => null),
          api.autoStatus().catch(() => null),
          api.compare().catch(() => null),
          api.orders().catch(() => []),
          api.exitConfig().catch(() => null),
          api.outcomes().catch(() => null),
          api.notifications().catch(() => ({ items: [] })),
        ]);
      setData({ health, cash, portfolio, auto, compare, orders, exit, outcomes, alerts });
      setLocked(false);
    } catch (e) {
      if (e instanceof Unauthorized) { setLocked(true); }
      else { console.warn('load failed', e); }
    }
  }, []);

  useEffect(() => {
    (async () => {
      await loadCreds();
      if (!hasPass()) setLocked(true); else await load();
      setBooted(true);
    })();
  }, [load]);

  const onRefresh = useCallback(async () => {
    setRefreshing(true); await load(); setRefreshing(false);
  }, [load]);

  // Poll while the app is open. 30s matches the web dashboard.
  useEffect(() => {
    if (locked || !booted) return;
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [locked, booted, load]);

  const unlock = async () => {
    if (!entry.trim()) return;
    setBusy(true); setErr('');
    await setPass(entry.trim());
    try { await api.cash(); setLocked(false); setEntry(''); await load(); }
    catch (e) {
      await setPass(null);
      setErr(e instanceof Unauthorized ? 'That passphrase was not accepted.' : 'Could not reach the server.');
    }
    setBusy(false);
  };

  // Cold start (app was killed) and warm tap are different APIs: the listener
  // only fires while the app is running, so a notification that launched the
  // app would otherwise land on Home.
  useEffect(() => {
    const sub = Notifications.addNotificationResponseReceivedListener(goToAlerts);
    Notifications.getLastNotificationResponseAsync()
      .then(r => { if (r) setTimeout(goToAlerts, 350); })
      .catch(() => {});
    return () => sub.remove();
  }, []);

  if (!booted) {
    return <View style={{ flex: 1, backgroundColor: t.bg, justifyContent: 'center' }}>
      <ActivityIndicator color={t.accent} /></View>;
  }

  if (locked) {
    return (
      <SafeAreaView style={{ flex: 1, backgroundColor: t.bg, justifyContent: 'center', padding: 24 }}>
        <StatusBar barStyle={t.dark ? 'light-content' : 'dark-content'} />
        <View style={{ alignItems: 'center', marginBottom: 22 }}>
          <Image source={require('./assets/icon.png')}
                 style={{ width: 72, height: 72, borderRadius: 16 }} />
          <Text style={{ color: t.head, fontSize: 21, fontWeight: '700', marginTop: 12 }}>RaanuBot</Text>
          <Text style={{ color: t.muted, fontSize: 15, marginTop: 6, textAlign: 'center' }}>
            Enter your passphrase to view the dashboard.
          </Text>
        </View>
        <TextInput
          value={entry} onChangeText={setEntry} placeholder="Passphrase"
          placeholderTextColor={t.muted} autoCapitalize="none" autoCorrect={false} secureTextEntry
          onSubmitEditing={unlock}
          style={{ backgroundColor: t.card, borderColor: t.line, borderWidth: 1, borderRadius: 10,
                   padding: 14, fontSize: 16, color: t.text, marginBottom: 12 }}
        />
        {!!err && <Text style={{ color: t.red, marginBottom: 10, fontSize: 14 }}>{err}</Text>}
        <Pressable onPress={unlock} disabled={busy}
          style={{ backgroundColor: busy ? t.muted : t.accent, borderRadius: 10, padding: 15, alignItems: 'center' }}>
          <Text style={{ color: '#fff', fontSize: 16, fontWeight: '600' }}>{busy ? 'Checking…' : 'Unlock'}</Text>
        </Pressable>
      </SafeAreaView>
    );
  }

  const props = { refreshing, onRefresh, data };

  return (
    <SafeAreaProvider>
      <ThemeCtx.Provider value={t}>
        <StatusBar barStyle={t.dark ? 'light-content' : 'dark-content'} />
        <Tabs t={t} props={props} />
      </ThemeCtx.Provider>
    </SafeAreaProvider>
  );
}

/**
 * Separate component so useSafeAreaInsets() can run — the hook must sit below
 * SafeAreaProvider, which App itself renders.
 *
 * Android 16 makes edge-to-edge mandatory, so the tab bar draws underneath the
 * system navigation bar unless its height and padding account for the bottom
 * inset. Without this the labels collide with the gesture pill or the
 * three-button nav.
 */
function Tabs({ t, props }: any) {
  const insets = useSafeAreaInsets();
  const barBase = Platform.OS === 'ios' ? 56 : 60;

  const navTheme = {
    ...(t.dark ? DarkTheme : DefaultTheme),
    colors: { ...(t.dark ? DarkTheme : DefaultTheme).colors,
      background: t.bg, card: t.card, text: t.head, border: t.line, primary: t.accent },
  };

  return (
        <NavigationContainer ref={navRef} theme={navTheme}>
          <Tab.Navigator
            screenOptions={({ route }) => ({
              headerStyle: { backgroundColor: t.card, borderBottomColor: t.line },
              // The brand reads better than the screen name — every tab is
              // already labelled in the bar below, so repeating it up here
              // said nothing.
              // Two-tone wordmark rather than a plain string: "Raanu" in the
              // heading colour, "Bot" in the brand purple, matching the logo
              // and the web header.
              headerTitle: () => (
                <Text style={{ fontSize: 23, fontWeight: '800', letterSpacing: -0.3 }}>
                  <Text style={{ color: t.head }}>Raanu</Text>
                  <Text style={{ color: t.accent }}>Bot</Text>
                </Text>
              ),
              headerTitleAlign: 'center',
              // Brand mark top-left on every screen. The robot alone, not the
              // full badge — the wordmark is unreadable at 34px.
              headerLeft: () => (
                <Image source={require('./assets/mark.png')}
                       style={{ width: 34, height: 34, borderRadius: 9, marginLeft: 14, marginRight: 2 }} />
              ),
              // Alerts lives in the header, not the tab bar: a sixth bottom tab
              // clips every label at 375pt. It is still a navigator screen so
              // a notification tap can route straight to it.
              headerRight: () => (
                <Pressable onPress={() => navRef.navigate('Alerts' as never)}
                           hitSlop={10} style={{ marginRight: 16 }}>
                  <Svg width={21} height={21} viewBox="0 0 24 24" fill="none"
                       stroke={t.head} strokeWidth={1.8} strokeLinecap="round"
                       strokeLinejoin="round">
                    <Path d="M18 8a6 6 0 10-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9" />
                    <Path d="M10.3 21a2 2 0 003.4 0" />
                  </Svg>
                </Pressable>
              ),
              // Taller than the default: at 62px the labels were clipped on
              // Android, which looked like a rendering fault rather than a
              // layout one.
              tabBarStyle: { backgroundColor: t.card, borderTopColor: t.line,
                             height: barBase + insets.bottom,
                             paddingTop: 8,
                             paddingBottom: insets.bottom > 0 ? insets.bottom : 10 },
              tabBarActiveTintColor: t.accent,
              tabBarInactiveTintColor: t.muted,
              tabBarLabelStyle: { fontSize: 12.5, marginBottom: 2 },
              tabBarIcon: ({ color }) => icons[route.name]?.(color),
            })}
          >
            <Tab.Screen name="Home">{() => <Dashboard {...props} />}</Tab.Screen>
            <Tab.Screen name="Orders">{() => <Orders {...props} />}</Tab.Screen>
            <Tab.Screen name="Signals">{() => <Signals {...props} />}</Tab.Screen>
            <Tab.Screen name="Strategy">{() => <Strategy {...props} />}</Tab.Screen>
            <Tab.Screen name="Logs">{() => <Logs {...props} />}</Tab.Screen>
            <Tab.Screen name="Alerts"
                        options={{ tabBarButton: () => null }}>
              {() => <Alerts {...props} />}
            </Tab.Screen>
          </Tab.Navigator>
        </NavigationContainer>
  );
}
