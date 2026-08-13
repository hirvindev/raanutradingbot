import React from 'react';
import { View, Text, ScrollView, RefreshControl } from 'react-native';
import { money, pct, signed } from '../format';
import { Card, H2, Muted, PL, useTheme, styles } from '../ui';
import { STRAT } from '../theme';

/**
 * Strategy — budgets and exit rules, the "how is it configured" screen.
 *
 * Shows each strategy's live budget alongside its realised record, so the
 * capital a strategy gets and the results it has earned sit next to each other.
 * That pairing is the point: S3 carries the largest share precisely because it
 * is the only one profitable in both halves of the backtest.
 */
export default function Strategy({ refreshing, onRefresh, data }: any) {
  const t = useTheme();
  const cmp = data?.compare || {};
  const auto = data?.auto || {};
  const exit = data?.exit || {};
  const cfg = auto.config || {};

  return (
    <ScrollView style={{ backgroundColor: t.bg }} contentContainerStyle={styles.pad}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={t.accent} />}>

      <H2 sub="capital and attempts, per strategy">Budgets</H2>
      {(['s3', 's1', 's2'] as const).map(k => {
        const st = cmp[k] || {};
        const meta = STRAT[k];
        const cap = (cfg.per_trade_max_by_strategy || {})[k];
        const lim = (cfg.weekly_limit_by_strategy || {})[k];
        const left = (auto.trades_remaining_by_strategy || {})[k];
        return (
          <Card key={k}>
            <View style={{ padding: 14 }}>
              <View style={[styles.rowBetween, { marginBottom: 10 }]}>
                <View style={{ borderLeftWidth: 3, borderLeftColor: (t as any)[meta.key], paddingLeft: 8 }}>
                  <Text style={{ color: t.head, fontWeight: '700', fontSize: 16.5 }}>{meta.name}</Text>
                </View>
                {st.closed_trades > 0 && <PL value={st.net_pnl || 0} size={16} />}
              </View>
              <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 14 }}>
                {[
                  ['Per trade', cap != null ? money(cap) : '—'],
                  ['Weekly limit', lim != null ? `${lim}` : '—'],
                  ['Left this week', left != null ? `${left}` : '—'],
                  ['Closed', `${st.closed_trades || 0}`],
                  ['Win rate', st.closed_trades ? `${st.win_rate}%` : '—'],
                ].map(([k2, v]) => (
                  <View key={k2 as string} style={{ minWidth: 92 }}>
                    <Muted style={{ fontSize: 12.5 }}>{k2}</Muted>
                    <Text style={{ color: t.head, fontSize: 15.5, fontWeight: '500', marginTop: 2 }}>{v}</Text>
                  </View>
                ))}
              </View>
            </View>
          </Card>
        );
      })}

      <H2 sub="how positions are closed">Exit rules</H2>
      <Card>
        <View style={{ padding: 14 }}>
          {[
            ['Stop mode', exit.stop_mode === 'atr' ? 'ATR-scaled' : 'fixed %'],
            ['Stop floor / ceiling', `${exit.stop_min_pct ?? '—'}% / ${exit.stop_max_pct ?? '—'}%`],
            ['Trail', `arms +${exit.trail_activate_atr ?? '—'}xATR, trails ${exit.trail_atr_mult ?? '—'}xATR`],
            ['Daily crash', `${exit.daily_crash_pct ?? '—'}%`],
            ['Check every', `${exit.profit_check_sec ?? cfg.profit_check_sec ?? '—'}s`],
          ].map(([k2, v]) => (
            <View key={k2 as string} style={[styles.rowBetween, { paddingVertical: 7 }]}>
              <Muted style={{ fontSize: 15 }}>{k2}</Muted>
              <Text style={{ color: t.head, fontSize: 15, fontWeight: '500' }}>{v}</Text>
            </View>
          ))}
        </View>
      </Card>
    </ScrollView>
  );
}
