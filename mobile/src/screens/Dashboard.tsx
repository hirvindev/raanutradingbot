import React from 'react';
import { View, Text, ScrollView, RefreshControl, ActivityIndicator } from 'react-native';
import { api } from '../api';
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

  const openPl = pos.reduce((a, p) => a + (p.ppl || 0), 0);
  const cost = pos.reduce((a, p) => a + (p.averagePrice || 0) * p.quantity, 0);
  const plPct = cost ? (openPl / cost) * 100 : 0;

  return (
    <ScrollView
      style={{ backgroundColor: t.bg }}
      contentContainerStyle={styles.pad}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={t.accent} />}
    >
      {/* Hero. One number readable at arm's length — the reason the app is open. */}
      <Card>
        <View style={{ padding: 20, alignItems: 'center' }}>
          <Text style={{ color: t.muted, fontSize: 12, letterSpacing: 1, textTransform: 'uppercase' }}>
            Portfolio value
          </Text>
          <Text style={{ color: t.head, fontSize: T.hero, fontWeight: '400', letterSpacing: -1, marginTop: 4,
                         fontVariant: ['tabular-nums'] }}>
            {money(cash.total)}
          </Text>
          <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8, marginTop: 6 }}>
            <PL value={cash.daily_ppl || 0} size={15} />
            <Muted style={{ fontSize: 13 }}>today</Muted>
          </View>
        </View>
      </Card>

      <View style={{ flexDirection: 'row', gap: 10, marginBottom: 18 }}>
        <View style={{ flex: 1, backgroundColor: t.card, borderColor: t.line, borderWidth: 1,
                       borderRadius: 10, padding: 13 }}>
          <Text style={{ color: t.muted, fontSize: 13, marginBottom: 5 }}>Open P&L</Text>
          <PL value={openPl} size={T.big} />
          <View style={{ flexDirection: 'row', alignItems: 'baseline', gap: 6, marginTop: 4 }}>
            <PL value={plPct} asPct size={13.5} weight="500" />
            <Muted style={{ fontSize: 13 }}>· {pos.length} pos</Muted>
          </View>
        </View>
        <View style={{ flex: 1, backgroundColor: t.card, borderColor: t.line, borderWidth: 1,
                       borderRadius: 10, padding: 13 }}>
          <Text style={{ color: t.muted, fontSize: 13, marginBottom: 5 }}>Free cash</Text>
          <Text numberOfLines={1} adjustsFontSizeToFit
                style={{ color: t.head, fontSize: T.big, fontWeight: '600',
                         fontVariant: ['tabular-nums'] }}>{money(cash.free)}</Text>
          <Muted style={{ fontSize: 13, marginTop: 4 }}>on {money(cost)} invested</Muted>
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
              <View key={p.ticker + i} style={{ padding: 13, borderBottomWidth: i === pos.length - 1 ? 0 : 1,
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
              </View>
            );
          })}
      </Card>
    </ScrollView>
  );
}
