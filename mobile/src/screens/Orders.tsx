import React, { useState, useMemo } from 'react';
import { View, Text, ScrollView, RefreshControl, Pressable } from 'react-native';
import { money, signed, pct, shortDate, sof } from '../format';
import { Card, Muted, PL, useTheme, Tag, styles } from '../ui';

/**
 * Orders — the month → day → strategy tree from the web dashboard.
 *
 * Two views: closed round-trips with realised P&L, and the raw order log.
 * Group headers carry their own totals so a collapsed level still says
 * something, which is what makes scanning a long history bearable on a phone.
 *
 * "Deployed" on the order-log headers counts BUYS ONLY. Summing both sides
 * double-counts a position — $500 in that came back as $983 is $500 of capital
 * used, not $1,483 traded — and the web version shipped that bug before it was
 * caught.
 */
export default function Orders({ refreshing, onRefresh, data }: any) {
  const t = useTheme();
  const [view, setView] = useState<'closed' | 'log'>('closed');
  const [shut, setShut] = useState<Record<string, boolean>>({});
  const toggle = (k: string) => setShut(s => ({ ...s, [k]: !s[k] }));

  const cmp = data?.compare || {};
  const orders: any[] = data?.orders || [];

  const closed = useMemo(() => {
    const out: any[] = [];
    for (const k of ['s1', 's2', 's3']) for (const r of cmp[k]?.closed || []) out.push(r);
    return out.sort((a, b) => String(b.sell_date || '').localeCompare(String(a.sell_date || '')));
  }, [cmp]);

  const groups = useMemo(() => {
    const rows = view === 'closed' ? closed : orders;
    const dayOf = (r: any) => {
      const d = view === 'closed' ? r.sell_date : r.created_at;
      if (!d) return 'unknown';
      const dt = new Date(d);
      return isNaN(+dt) ? 'unknown'
        : `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')}`;
    };
    const months: any[] = [];
    const byMonth = new Map<string, any>();
    for (const r of rows) {
      const d = dayOf(r), m = d === 'unknown' ? 'unknown' : d.slice(0, 7);
      if (!byMonth.has(m)) { byMonth.set(m, { m, days: [], byDay: new Map() }); months.push(byMonth.get(m)); }
      const M = byMonth.get(m);
      if (!M.byDay.has(d)) { M.byDay.set(d, []); M.days.push(d); }
      M.byDay.get(d).push(r);
    }
    return months;
  }, [view, closed, orders]);

  const val = (o: any) => {
    const q = parseFloat(o.filled_qty || o.qty || 0);
    const p = parseFloat(o.filled_avg_price || o.limit_price || 0);
    return p && q ? p * q : 0;
  };
  const deployed = (rs: any[]) => rs.reduce((a, o) => a + (o.side === 'buy' ? val(o) : 0), 0);
  const realised = (rs: any[]) => rs.reduce((a, r) => a + (r.pnl || 0), 0);

  return (
    <ScrollView style={{ backgroundColor: t.bg }} contentContainerStyle={styles.pad}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={t.accent} />}>

      <View style={{ flexDirection: 'row', gap: 8, marginBottom: 14 }}>
        {(['closed', 'log'] as const).map(v => (
          <Pressable key={v} onPress={() => setView(v)}
            style={{ paddingVertical: 10, paddingHorizontal: 14, borderRadius: 8, borderWidth: 1,
                     borderColor: view === v ? t.accent : t.line,
                     backgroundColor: view === v ? t.accentBg : t.card }}>
            <Text style={{ color: view === v ? t.accent : t.muted, fontSize: 15 }}>
              {v === 'closed' ? 'Closed P&L' : 'Order history'}
            </Text>
          </Pressable>
        ))}
      </View>

      {groups.length === 0 && <Muted style={{ padding: 20, textAlign: 'center' }}>Nothing here yet</Muted>}

      {groups.map(M => {
        const all = M.days.flatMap((d: string) => M.byDay.get(d));
        const mShut = shut['m' + M.m];
        const label = M.m === 'unknown' ? 'Unknown date'
          : new Date(M.m + '-01T00:00:00').toLocaleDateString('en-GB', { month: 'long', year: 'numeric' });
        return (
          <Card key={M.m}>
            <Pressable onPress={() => toggle('m' + M.m)}
              style={{ padding: 14, backgroundColor: t.accentBg, borderBottomWidth: mShut ? 0 : 1, borderBottomColor: t.line }}>
              <View style={styles.rowBetween}>
                <Text style={{ color: t.head, fontWeight: '700', fontSize: 16.5 }}>{label}</Text>
                {view === 'closed'
                  ? <PL value={realised(all)} size={16} />
                  : <Muted style={{ fontSize: 14 }}>{money(deployed(all))} deployed</Muted>}
              </View>
              <Muted style={{ fontSize: 13.5, marginTop: 3 }}>
                {all.length} {view === 'closed' ? 'position' : 'order'}{all.length === 1 ? '' : 's'}
              </Muted>
            </Pressable>

            {!mShut && M.days.map((d: string) => {
              const rs = M.byDay.get(d);
              const dShut = shut['d' + d];
              const dLabel = d === 'unknown' ? 'Unknown date'
                : new Date(d + 'T00:00:00').toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
              return (
                <View key={d}>
                  <Pressable onPress={() => toggle('d' + d)}
                    style={{ padding: 13, backgroundColor: t.wash, borderBottomWidth: 1, borderBottomColor: t.line }}>
                    <View style={styles.rowBetween}>
                      <Text style={{ color: t.head, fontWeight: '600', fontSize: 15.5 }}>{dLabel}</Text>
                      {view === 'closed'
                        ? <PL value={realised(rs)} size={15} />
                        : <Muted style={{ fontSize: 14 }}>{money(deployed(rs))}</Muted>}
                    </View>
                  </Pressable>

                  {!dShut && rs.map((r: any, i: number) => (
                    <View key={i} style={{ padding: 13, borderBottomWidth: 1, borderBottomColor: t.line2 }}>
                      <View style={styles.rowBetween}>
                        <View style={{ flexDirection: 'row', alignItems: 'center', flex: 1 }}>
                          <Text style={{ color: t.head, fontSize: 16, fontWeight: '600' }}>
                            {r.symbol || r.ticker}
                          </Text>
                          {!!sof(r) && <Tag strat={sof(r)} />}
                        </View>
                        {view === 'closed'
                          ? <PL value={r.pnl || 0} size={16} />
                          : <Text style={{ color: t.text, fontSize: 15.5, fontVariant: ['tabular-nums'] }}>
                              {money(val(r))}
                            </Text>}
                      </View>
                      <View style={[styles.rowBetween, { marginTop: 3 }]}>
                        <Muted style={{ fontSize: 13.5 }}>
                          {view === 'closed'
                            ? `${(r.qty || 0).toFixed(2)} @ ${money(r.buy_price || 0)} → ${money(r.sell_price || 0)}`
                            : `${(r.side || '').toUpperCase()} ${parseFloat(r.filled_qty || r.qty || 0).toFixed(2)} · ${r.status || ''}`}
                        </Muted>
                        {view === 'closed' && r.pct != null && <PL value={r.pct} asPct size={13.5} weight="500" />}
                      </View>
                    </View>
                  ))}
                </View>
              );
            })}
          </Card>
        );
      })}
    </ScrollView>
  );
}
