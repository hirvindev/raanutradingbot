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
import { NavigationContainer, DefaultTheme, DarkTheme } from '@react-navigation/native';
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import Svg, { Path, Circle } from 'react-native-svg';

import { api, loadCreds, setPass, hasPass, Unauthorized } from './src/api';
import { light, dark, type as T } from './src/theme';
import { ThemeCtx } from './src/ui';
import Dashboard from './src/screens/Dashboard';
import Orders from './src/screens/Orders';
import Signals from './src/screens/Signals';
import Strategy from './src/screens/Strategy';
import Logs from './src/screens/Logs';

const Tab = createBottomTabNavigator();

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
  const scheme = useColorScheme();
  const t = scheme === 'dark' ? dark : light;

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
      const [health, cash, portfolio, auto, compare, orders, exit, outcomes] =
        await Promise.all([
          api.health().catch(() => null),
          api.cash(),
          api.portfolio().catch(() => null),
          api.autoStatus().catch(() => null),
          api.compare().catch(() => null),
          api.orders().catch(() => []),
          api.exitConfig().catch(() => null),
          api.outcomes().catch(() => null),
        ]);
      setData({ health, cash, portfolio, auto, compare, orders, exit, outcomes });
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

  const navTheme = {
    ...(t.dark ? DarkTheme : DefaultTheme),
    colors: { ...(t.dark ? DarkTheme : DefaultTheme).colors,
      background: t.bg, card: t.card, text: t.head, border: t.line, primary: t.accent },
  };
  const props = { refreshing, onRefresh, data };

  return (
    <SafeAreaProvider>
      <ThemeCtx.Provider value={t}>
        <StatusBar barStyle={t.dark ? 'light-content' : 'dark-content'} />
        <NavigationContainer theme={navTheme}>
          <Tab.Navigator
            screenOptions={({ route }) => ({
              headerStyle: { backgroundColor: t.card, borderBottomColor: t.line },
              headerTitleStyle: { color: t.head, fontSize: 17 },
              // Taller than the default: at 62px the labels were clipped on
              // Android, which looked like a rendering fault rather than a
              // layout one.
              tabBarStyle: { backgroundColor: t.card, borderTopColor: t.line,
                             height: Platform.OS === 'ios' ? 88 : 72, paddingTop: 8, paddingBottom: 10 },
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
          </Tab.Navigator>
        </NavigationContainer>
      </ThemeCtx.Provider>
    </SafeAreaProvider>
  );
}
