import React, { useState, useEffect } from 'react';
import { View, Text, ScrollView, RefreshControl, Platform, useWindowDimensions,
         Pressable, Alert } from 'react-native';
import Constants from 'expo-constants';
import { Card, H2, Muted, useTheme, styles } from '../ui';
import { shortTime } from '../format';
import { host } from '../api';
import { pushState, enablePush, sendTest, PushState } from '../push';

/**
 * Logs — the bot's recent events, plus what this device is actually doing.
 *
 * The device block exists because a phone problem cost three rounds of guessing
 * on the web app: the layout was correct and deployed, but Chrome's "Desktop
 * site" setting made the viewport report ~980px and defeated every breakpoint.
 * A native app cannot hit that specific failure, but the principle stands —
 * when something looks wrong, the app should say what it sees.
 */
export default function Logs({ refreshing, onRefresh, data }: any) {
  const t = useTheme();
  const { width, height } = useWindowDimensions();
  const auto = data?.auto || {};
  const health = data?.health || {};
  const events: any[] = auto.recent_events || [];

  const [ps, setPs] = useState<PushState>('off');
  const [busy, setBusy] = useState(false);
  useEffect(() => { pushState().then(setPs); }, []);

  const onPush = async () => {
    setBusy(true);
    const r = await enablePush();
    setPs(await pushState());
    setBusy(false);
    // Say what happened either way. Silent success is indistinguishable from
    // silent failure, which is precisely how the web attempt looked broken.
    Alert.alert(r.ok ? 'Notifications on' : 'Not enabled', r.msg);
    if (r.ok) Alert.alert('Test', await sendTest());
  };

  const Row = ({ k, v, ok }: any) => (
    <View style={[styles.rowBetween, { paddingVertical: 6 }]}>
      <Muted style={{ fontSize: 15 }}>{k}</Muted>
      <Text style={{ fontSize: 15, fontWeight: '500',
                     color: ok === undefined ? t.head : ok ? t.green : t.red }}>{v}</Text>
    </View>
  );

  return (
    <ScrollView style={{ backgroundColor: t.bg }} contentContainerStyle={styles.pad}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={t.accent} />}>

      <H2 sub="what this device and the server are doing">Status</H2>
      <Card>
        <View style={{ padding: 14 }}>
          <Row k="Server" v={health.status || '—'} ok={health.status === 'ok'} />
          <Row k="Mode" v={(health.mode || '—').toUpperCase()} ok={health.mode === 'paper'} />
          <Row k="Auto-trader" v={auto.enabled ? 'ON' : 'off'} ok={!!auto.enabled} />
          <Row k="State storage" v={health.state?.data_dir || '—'} ok={!!health.state?.persistent} />
          <Row k="Host" v={host().replace('https://', '')} />
          <Row k="Screen" v={`${Math.round(width)} x ${Math.round(height)}`} />
          <Row k="App" v={`${Constants.expoConfig?.version || '1.0.0'} · ${Platform.OS}`} />
        </View>
      </Card>

      <H2 sub="buys, exits and errors only">Notifications</H2>
      <Card>
        <View style={{ padding: 14 }}>
          <Row k="Status" v={{on:'On', off:'Off', blocked:'Blocked in settings',
                              unsupported:'Not available', 'no-fcm':'Needs Firebase config'}[ps]}
               ok={ps === 'on'} />
          <Muted style={{ fontSize: 14, marginTop: 6, lineHeight: 20 }}>
            Only buys, exits and errors are pushed. Scans and quiet days are not —
            Telegram keeps the full record.
          </Muted>
          <Pressable onPress={onPush} disabled={busy}
            style={{ marginTop: 12, backgroundColor: busy ? t.muted : t.accent,
                     borderRadius: 10, padding: 13, alignItems: 'center' }}>
            <Text style={{ color: '#fff', fontSize: 15.5, fontWeight: '600' }}>
              {busy ? 'Working…' : ps === 'on' ? 'Send a test notification' : 'Turn on notifications'}
            </Text>
          </Pressable>
        </View>
      </Card>

      <H2 sub="most recent first">Engine events</H2>
      <Card>
        {events.length === 0 && <Muted style={{ padding: 20, textAlign: 'center' }}>No events yet</Muted>}
        {[...events].reverse().map((e, i) => (
          <View key={i} style={{ padding: 12, paddingHorizontal: 14,
                                 borderBottomWidth: i === events.length - 1 ? 0 : 1, borderBottomColor: t.line2 }}>
            <View style={styles.rowBetween}>
              <Text style={{ color: e.kind === 'error' ? t.red : e.kind === 'buy' ? t.green : t.muted,
                             fontSize: 12.5, fontWeight: '600', textTransform: 'uppercase' }}>{e.kind}</Text>
              <Muted style={{ fontSize: 13 }}>{shortTime(e.ts)}</Muted>
            </View>
            <Text style={{ color: t.text, fontSize: 14.5, marginTop: 3, lineHeight: 20 }}>{e.msg}</Text>
          </View>
        ))}
      </Card>
    </ScrollView>
  );
}
