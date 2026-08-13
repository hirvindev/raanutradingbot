import React, { useState } from 'react';
import { View, Text, ScrollView, RefreshControl, ActivityIndicator,
         Pressable, Alert, Modal, TextInput } from 'react-native';
import { api, tradeAction } from '../api';
import { money, signed, pct, isUp, sof } from '../format';
import { Card, H2, Muted, PL, useTheme, Tag, styles } from '../ui';
import { type as T } from '../theme';

/**
 * Dashboard — "am I up or down, and what is it holding?"
 *
 * One hero figure, a supporting row, then the positions. Positions use the
 * three-line layout the web app settled on: context, then identity and the
 * number that matters, then the detail you check second. Everything visible
 * without tapping, because on a list this short a disclosure gesture buys
 * nothing.
 */
export default function Dashboard({ refreshing, onRefresh, data }: any) {
  const t = useTheme();
  const { cash, portfolio } = data || {};
  const pos: any[] = portfolio?.positions || (Array.isArray(portfolio) ? portfolio : []) || [];

  if (!cash) {
    return <View style={{ flex: 1, justifyContent: 'center' }}><ActivityIndicator color={t.accent} /></View>;
  }

  // Exit flow: confirm the specific position, then ask for the trade PIN.
  // Two steps on purpose — one tap should never be able to close a position,
  // and the PIN is held only for the duration of the request.
  const [exiting, setExiting] = useState<any>(null);
  const [pin, setPin] = useState('');
  const [busy, setBusy] = useState(false);

  const confirmExit = (p: any) => {
    Alert.alert(
      `Exit ${p.ticker}?`,
      `Sell ${p.quantity.toFixed(2)} at market.\nCurrent P&L ${signed(p.ppl || 0)}.`,
      [{ text: 'Cancel', style: 'cancel' },
       { text: 'Exit position', style: 'destructive', onPress: () => { setPin(''); setExiting(p); } }],
    );
  };

  const doExit = async () => {
    if (!pin.trim() || !exiting) return;
    setBusy(true);
    const r = await tradeAction('/api/orders/sell', pin.trim(),
                                { ticker: exiting.ticker, quantity: exiting.quantity });
    setBusy(false);
    if (r.status === 403) Alert.alert('Rejected', 'That trade PIN was not accepted.');
    else if (r.ok) { Alert.alert('Exit submitted', `${exiting.ticker} sell order placed.`); onRefresh?.(); }
    else Alert.alert('Failed', (r.data as any)?.detail || `Server returned ${r.status}`);
    setExiting(null); setPin('');
  };

  const openPl = pos.reduce((a, p) => a + (p.ppl || 0), 0);
  const cost = pos.reduce((a, p) => a + (p.averagePrice || 0) * p.quantity, 0);
  const curValue = pos.reduce((a, p) => a + (p.currentPrice ?? p.averagePrice ?? 0) * p.quantity, 0);
  const plPct = cost ? (openPl / cost) * 100 : 0;

  return (
    <ScrollView
      style={{ backgroundColor: t.bg }}
      contentContainerStyle={styles.pad}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={t.accent} />}
    >
      {/* Invested against current, then the difference. One number in isolation
          ("portfolio value") cannot say whether you are up: it needs the cost
          basis beside it. This answers what went in, what it is worth now, and
          what that is in money and percent — in one glance. */}
      <Card>
        <View style={{ padding: 16 }}>
          <View style={styles.rowBetween}>
            <View>
              <Muted style={{ fontSize: 13.5 }}>Invested</Muted>
              <Text style={{ color: t.head, fontSize: 20, fontWeight: '500', marginTop: 3,
                             fontVariant: ['tabular-nums'] }}>{money(cost)}</Text>
            </View>
            <View style={{ alignItems: 'flex-end' }}>
              <Muted style={{ fontSize: 13.5 }}>Current</Muted>
              <Text style={{ color: t.head, fontSize: 20, fontWeight: '500', marginTop: 3,
                             fontVariant: ['tabular-nums'] }}>{money(curValue)}</Text>
            </View>
          </View>

          <View style={{ height: 1, backgroundColor: t.line, marginVertical: 13 }} />

          <View style={styles.rowBetween}>
            <Text style={{ color: t.head, fontSize: 16.5, fontWeight: '600' }}>
              {openPl >= 0 ? 'Profit' : 'Loss'}
            </Text>
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 9 }}>
              <PL value={openPl} size={21} />
              <View style={{ paddingHorizontal: 9, paddingVertical: 3, borderRadius: 20,
                             backgroundColor: (openPl >= 0 ? t.green : t.red) + '22' }}>
                <PL value={plPct} asPct size={13.5} weight="600" />
              </View>
            </View>
          </View>
        </View>
      </Card>

      <View style={{ flexDirection: 'row', gap: 10, marginBottom: 18 }}>
        <View style={{ flex: 1, backgroundColor: t.card, borderColor: t.line, borderWidth: 1,
                       borderRadius: 10, padding: 13 }}>
          <Text style={{ color: t.muted, fontSize: 13, marginBottom: 5 }}>Today</Text>
          <PL value={cash.daily_ppl || 0} size={T.big} />
          <Muted style={{ fontSize: 13, marginTop: 4 }}>{pos.length} position{pos.length === 1 ? '' : 's'}</Muted>
        </View>
        <View style={{ flex: 1, backgroundColor: t.card, borderColor: t.line, borderWidth: 1,
                       borderRadius: 10, padding: 13 }}>
          <Text style={{ color: t.muted, fontSize: 13, marginBottom: 5 }}>Free cash</Text>
          <Text numberOfLines={1} adjustsFontSizeToFit
                style={{ color: t.head, fontSize: T.big, fontWeight: '600',
                         fontVariant: ['tabular-nums'] }}>{money(cash.free)}</Text>
          <Muted style={{ fontSize: 13, marginTop: 4 }}>of {money(cash.total)} total</Muted>
        </View>
      </View>

      <H2 sub={pos.length ? undefined : 'nothing open'}>Positions</H2>
      <Card>
        {pos.length === 0 && <Muted style={{ padding: 20, textAlign: 'center' }}>No open positions</Muted>}
        {[...pos]
          .sort((a, b) => (b.currentPrice || b.averagePrice) * b.quantity - (a.currentPrice || a.averagePrice) * a.quantity)
          .map((p, i) => {
            const ltp = p.currentPrice ?? p.averagePrice;
            const pl = p.ppl ?? (ltp - p.averagePrice) * p.quantity;
            const ch = ((ltp - p.averagePrice) / p.averagePrice) * 100;
            return (
              <Pressable key={p.ticker + i} onLongPress={() => confirmExit(p)} delayLongPress={450}
                style={{ padding: 13, borderBottomWidth: i === pos.length - 1 ? 0 : 1,
                         borderBottomColor: t.line2 }}>
                <View style={styles.rowBetween}>
                  <Muted style={{ fontSize: 14 }}>Qty {p.quantity.toFixed(2)}  ·  Avg {money(p.averagePrice)}</Muted>
                  <PL value={ch} asPct size={14.5} weight="500" />
                </View>
                <View style={[styles.rowBetween, { marginTop: 2 }]}>
                  <View style={{ flexDirection: 'row', alignItems: 'center', flex: 1 }}>
                    <Text style={{ color: t.head, fontSize: 18.5, fontWeight: '600' }}>{p.ticker}</Text>
                    {!!sof(p) && <Tag strat={sof(p)} />}
                  </View>
                  <PL value={pl} size={18.5} />
                </View>
                <View style={[styles.rowBetween, { marginTop: 2 }]}>
                  <Muted style={{ fontSize: 14 }}>Value {money(ltp * p.quantity)}</Muted>
                  <Muted style={{ fontSize: 14 }}>LTP {money(ltp)}</Muted>
                </View>
              </Pressable>
            );
          })}
        {pos.length > 0 && (
          <Muted style={{ fontSize: 13, textAlign: 'center', paddingVertical: 10 }}>
            Press and hold a position to exit it
          </Muted>
        )}
      </Card>

      <Modal visible={!!exiting} transparent animationType="fade"
             onRequestClose={() => setExiting(null)}>
        <View style={{ flex: 1, backgroundColor: '#0008', justifyContent: 'center', padding: 26 }}>
          <View style={{ backgroundColor: t.card, borderRadius: 14, padding: 20 }}>
            <Text style={{ color: t.head, fontSize: 18, fontWeight: '700' }}>
              Exit {exiting?.ticker}
            </Text>
            <Muted style={{ fontSize: 14.5, marginTop: 6, lineHeight: 20 }}>
              Enter your trade PIN. It is used for this order only and never saved.
            </Muted>
            <TextInput value={pin} onChangeText={setPin} placeholder="Trade PIN"
              placeholderTextColor={t.muted} secureTextEntry autoCapitalize="characters"
              style={{ backgroundColor: t.bg, borderColor: t.line, borderWidth: 1, borderRadius: 10,
                       padding: 13, fontSize: 16, color: t.text, marginTop: 14 }} />
            <View style={{ flexDirection: 'row', gap: 10, marginTop: 14 }}>
              <Pressable onPress={() => setExiting(null)} style={{ flex: 1, padding: 13, borderRadius: 10,
                         borderWidth: 1, borderColor: t.line, alignItems: 'center' }}>
                <Text style={{ color: t.muted, fontSize: 15.5 }}>Cancel</Text>
              </Pressable>
              <Pressable onPress={doExit} disabled={busy}
                style={{ flex: 1, padding: 13, borderRadius: 10, alignItems: 'center',
                         backgroundColor: busy ? t.muted : t.red }}>
                <Text style={{ color: '#fff', fontSize: 15.5, fontWeight: '600' }}>
                  {busy ? 'Selling…' : 'Sell at market'}
                </Text>
              </Pressable>
            </View>
          </View>
        </View>
      </Modal>
    </ScrollView>
  );
}
