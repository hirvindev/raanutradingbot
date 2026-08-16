import React from 'react';
import { View, Text, ScrollView, RefreshControl, ActivityIndicator,
         Platform } from 'react-native';
import { Card, Muted, useTheme, styles } from '../ui';

/**
 * Alerts — every notification from the last 48 hours.
 *
 * Android dismisses a notification the moment it is tapped, which is fine for a
 * reminder and wrong for a trade signal: the alert carried the entry, the stop
 * and the reasoning, and it was the only copy. This is the copy.
 *
 * The body is rendered monospace and unwrapped-as-written because it is
 * column-aligned text — a proportional font throws that alignment away and the
 * snapshot stops being scannable.
 */
export default function Alerts({ refreshing, onRefresh, data }: any) {
  const t = useTheme();
  const items: any[] = data?.alerts?.items || [];

  if (!data?.alerts) {
    return <View style={{ flex: 1, backgroundColor: t.bg, justifyContent: 'center' }}>
      <ActivityIndicator color={t.accent} /></View>;
  }

  const when = (ts: string) => {
    const d = new Date(ts);
    return d.toLocaleString(undefined,
      { weekday: 'short', hour: '2-digit', minute: '2-digit' });
  };

  return (
    <ScrollView
      style={{ backgroundColor: t.bg }}
      contentContainerStyle={styles.pad}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh}
                                      tintColor={t.accent} />}
    >
      <View style={[styles.rowBetween, { marginBottom: 10, paddingHorizontal: 2 }]}>
        <Text style={{ color: t.head, fontSize: 20, fontWeight: '700' }}>Alerts</Text>
        <Muted style={{ fontSize: 13 }}>
          {items.length ? `${items.length} in 48h` : 'kept 48 hours'}
        </Muted>
      </View>

      <Card>
        {items.length === 0 && (
          <Muted style={{ padding: 22, textAlign: 'center' }}>
            No alerts in the last 48 hours
          </Muted>
        )}
        {items.map((a, i) => (
          <View key={(a.ts || '') + i}
                style={{ padding: 14,
                         borderBottomWidth: i === items.length - 1 ? 0 : 1,
                         borderBottomColor: t.line2 }}>
            <Text style={{ color: t.head, fontSize: 15, fontWeight: '600' }}>
              {a.title}
            </Text>
            <Muted style={{ fontSize: 12, marginTop: 2 }}>{when(a.ts)}</Muted>
            {/* 'monospace' is an Android family name. On iOS it does not
                resolve and silently falls back to the system font, which
                throws away the column alignment this text depends on. */}
            <Text style={{ color: t.text, fontSize: 12.5, lineHeight: 19, marginTop: 8,
                           fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace' }}>
              {a.body}
            </Text>
          </View>
        ))}
      </Card>
    </ScrollView>
  );
}
