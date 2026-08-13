/**
 * ui.tsx — the handful of primitives every screen shares.
 *
 * Kept small on purpose: a Card, a Row, a Stat and a P&L-coloured number cover
 * almost all of this app. Anything more elaborate would be a component library
 * for five screens.
 */
import React, { createContext, useContext } from 'react';
import { Text, View, StyleSheet, ViewStyle, TextStyle } from 'react-native';
import { Theme, type as T } from './theme';
import { isUp, signed, pct } from './format';

export const ThemeCtx = createContext<Theme>(null as any);
export const useTheme = () => useContext(ThemeCtx);

export const Card: React.FC<{ style?: ViewStyle; children: React.ReactNode }> = ({ style, children }) => {
  const t = useTheme();
  return (
    <View style={[{
      backgroundColor: t.card, borderColor: t.line, borderWidth: 1,
      borderRadius: 12, marginBottom: 14, overflow: 'hidden',
    }, style]}>{children}</View>
  );
};

export const H2: React.FC<{ children: React.ReactNode; sub?: string }> = ({ children, sub }) => {
  const t = useTheme();
  return (
    <View style={{ marginBottom: 10, marginTop: 4 }}>
      <Text style={{ fontSize: T.title, fontWeight: '600', color: t.head }}>{children}</Text>
      {!!sub && <Text style={{ fontSize: T.small, color: t.muted, marginTop: 2 }}>{sub}</Text>}
    </View>
  );
};

export const Muted: React.FC<{ children: React.ReactNode; style?: TextStyle }> = ({ children, style }) => {
  const t = useTheme();
  return <Text style={[{ color: t.muted, fontSize: T.small }, style]}>{children}</Text>;
};

/** A signed money or percentage figure, coloured by sign.
 *  Green and red mean exactly one thing in this app: profit and loss. Order
 *  size, quantities and prices stay neutral — an early web build coloured a
 *  buy red for "cash out" and it read as a loss. */
export const PL: React.FC<{ value: number; asPct?: boolean; size?: number; weight?: TextStyle['fontWeight'] }> =
({ value, asPct, size = T.row, weight = '600' }) => {
  const t = useTheme();
  return (
    <Text style={{ color: isUp(value) ? t.green : t.red, fontSize: size, fontWeight: weight,
                   fontVariant: ['tabular-nums'] }}>
      {asPct ? pct(value) : signed(value)}
    </Text>
  );
};

export const Divider = () => {
  const t = useTheme();
  return <View style={{ height: 1, backgroundColor: t.line }} />;
};

export const Tag: React.FC<{ strat: string }> = ({ strat }) => {
  const t = useTheme();
  const colour = strat === 's1' ? t.s1 : strat === 's2' ? t.s2 : strat === 's3' ? t.s3 : t.muted;
  return (
    <View style={{ borderLeftWidth: 3, borderLeftColor: colour, paddingLeft: 6, marginLeft: 8 }}>
      <Text style={{ color: t.muted, fontSize: 12.5 }}>{strat ? strat.toUpperCase() : 'Untagged'}</Text>
    </View>
  );
};

export const styles = StyleSheet.create({
  screen: { flex: 1 },
  pad: { padding: 14, paddingBottom: 28 },
  rowBetween: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
});
