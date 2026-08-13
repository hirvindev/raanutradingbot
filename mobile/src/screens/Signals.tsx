import React from 'react';
import { View, Text, ScrollView, RefreshControl } from 'react-native';
import { money, pct, nextScan, shortTime } from '../format';
import { Card, H2, Muted, PL, useTheme, styles } from '../ui';
import { type as T, STRAT } from '../theme';

/**
 * Signals — the learning screen, and the reason this app exists.
 *
 * Two questions: is the bot scanning on time, and WHY was each name a good
 * swing candidate. The scoring engines already produce full sentences
 * explaining every pick; the web dashboard threw them away for months and
 * printed "HRL 90". They are shown here in full — reading them is the point.
 *
 * Track record sits underneath, because "why did it pick this" is only half a
 * lesson without "and was it right".
 */
export default function Signals({ refreshing, onRefresh, data }: any) {
  const t = useTheme();
  const cmp = data?.compare || {};
  const out = data?.outcomes || {};
  const n = nextScan();
  const summary = out.summary || {};
  const bands = Object.entries(summary.by_score_band || {}) as [string, any][];

  return (
    <ScrollView
      style={{ backgroundColor: t.bg }}
      contentContainerStyle={styles.pad}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={t.accent} />}
    >
      <Card>
        <View style={{ padding: 14 }}>
          <Text style={{ color: t.text, fontSize: 16 }}>
            <Muted style={{ fontSize: 16 }}>Next scan </Muted>
            <Text style={{ color: t.head, fontWeight: '700' }}>{n ? n.label : '—'}</Text>
            <Muted style={{ fontSize: 16 }}>  in {n ? n.inText : '—'}</Muted>
          </Text>
          <Muted style={{ fontSize: 13.5, marginTop: 5 }}>Runs 09:35 and 11:00 ET, weekdays</Muted>
        </View>
      </Card>

      {/* S3 first — it is the only strategy profitable in both halves of the
          backtest, so it is the one worth learning from. */}
      {(['s3', 's1', 's2'] as const).map(k => {
        const picks: any[] = cmp[k + '_picks'] || [];
        const at = cmp[k + '_scanned_at'];
        const meta = STRAT[k];
        return (
          <View key={k}>
            <View style={[styles.rowBetween, { marginTop: 16, marginBottom: 8 }]}>
              <View style={{ borderLeftWidth: 3, borderLeftColor: (t as any)[meta.key], paddingLeft: 8 }}>
                <Text style={{ color: t.head, fontWeight: '600', fontSize: 15.5 }}>{meta.name}</Text>
              </View>
              <Muted style={{ fontSize: 13 }}>{at ? `scanned ${shortTime(at)}` : 'no scan yet'}</Muted>
            </View>

            {picks.length === 0 && (
              <Muted style={{ fontSize: 15, paddingVertical: 8 }}>
                Nothing met this strategy's conditions
              </Muted>
            )}

            {picks.slice(0, 5).map((p, i) => (
              <Card key={p.ticker + i}>
                <View style={{ padding: 13 }}>
                  <View style={styles.rowBetween}>
                    <View style={{ flex: 1 }}>
                      <Text style={{ color: t.head, fontSize: 18, fontWeight: '600' }}>{p.ticker}</Text>
                      {!!p.name && p.name !== p.ticker && (
                        <Muted style={{ fontSize: 14 }}>{p.name}</Muted>
                      )}
                    </View>
                    <Text style={{ fontSize: 23, fontWeight: '700',
                                   color: p.score >= 75 ? t.green : t.muted,
                                   fontVariant: ['tabular-nums'] }}>{p.score}</Text>
                  </View>
                  {(p.reasons || []).map((r: string, j: number) => (
                    <View key={j} style={{ flexDirection: 'row', marginTop: 5 }}>
                      <Text style={{ color: t.accent, fontWeight: '700', marginRight: 7 }}>·</Text>
                      <Text style={{ color: t.text, fontSize: 15, lineHeight: 22, flex: 1 }}>{r}</Text>
                    </View>
                  ))}
                </View>
              </Card>
            ))}
          </View>
        );
      })}

      <H2 sub="what past picks actually did">Track record</H2>
      <Card>
        <View style={{ padding: 14, borderBottomWidth: 1, borderBottomColor: t.line }}>
          <Text style={{ color: t.head, fontSize: 16 }}>
            <Text style={{ fontWeight: '700' }}>{summary.total_picks || 0}</Text>
            <Muted style={{ fontSize: 16 }}> picks logged · {summary.matured_5d || 0} with a 5-day result</Muted>
          </Text>
          <Muted style={{ fontSize: 14, marginTop: 6, lineHeight: 20 }}>{summary.verdict || ''}</Muted>
        </View>

        {bands.length > 0 && (
          <View style={{ padding: 14, paddingTop: 10 }}>
            <View style={[styles.rowBetween, { marginBottom: 6 }]}>
              <Muted style={{ fontSize: 12.5, flex: 1 }}>SCORE</Muted>
              <Muted style={{ fontSize: 12.5, width: 78, textAlign: 'right' }}>5-DAY</Muted>
              <Muted style={{ fontSize: 12.5, width: 74, textAlign: 'right' }}>VS SPY</Muted>
            </View>
            {bands.map(([label, v]) => (
              <View key={label} style={[styles.rowBetween, { paddingVertical: 6 }]}>
                <Text style={{ color: t.head, fontSize: 15, flex: 1 }}>{label}</Text>
                <View style={{ width: 78, alignItems: 'flex-end' }}>
                  {v.d5 ? <PL value={v.d5.avg} asPct size={15} weight="500" />
                        : <Muted style={{ fontSize: 15 }}>—</Muted>}
                </View>
                <View style={{ width: 74, alignItems: 'flex-end' }}>
                  {v.d5 && v.d5.edge != null
                    ? <PL value={v.d5.edge} asPct size={15} weight="500" />
                    : <Muted style={{ fontSize: 15 }}>—</Muted>}
                </View>
              </View>
            ))}
          </View>
        )}

        {(out.recent || []).slice(0, 12).map((r: any, i: number) => (
          <View key={i} style={{ padding: 12, paddingHorizontal: 14, borderTopWidth: 1, borderTopColor: t.line2 }}>
            <View style={styles.rowBetween}>
              <View>
                <Text style={{ color: t.head, fontSize: 15.5, fontWeight: '600' }}>{r.ticker}</Text>
                <Muted style={{ fontSize: 13 }}>{r.date} · {r.strategy?.toUpperCase()} · score {r.score}</Muted>
              </View>
              {r.fwd?.d5 != null
                ? <PL value={r.fwd.d5} asPct size={15.5} />
                : <Muted style={{ fontSize: 15 }}>maturing</Muted>}
            </View>
          </View>
        ))}
      </Card>
    </ScrollView>
  );
}
