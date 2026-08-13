/**
 * theme.ts — the same palette the web dashboard uses.
 *
 * Ported deliberately rather than reinvented: two clients reading the same
 * account should not disagree about what "profit" looks like. The values are
 * the CSS custom properties from RaanuTradingBot.html.
 *
 * Dark is not an inversion of light. The brand purple (#6941c6) fails contrast
 * on a dark ground so the accent lightens; profit and loss shift to slightly
 * brighter, less saturated variants; and surfaces are dark aubergine rather
 * than true black, because on OLED black the thin borders this layout depends
 * on vanish completely.
 */
export type Theme = typeof light;

export const light = {
  dark: false,
  bg: '#f7f4fc',
  card: '#ffffff',
  line: '#e0d7f2',
  line2: '#eee8fa',
  wash: '#faf7ff',
  text: '#443a5e',
  head: '#2b1a52',
  muted: '#8b81a3',
  accent: '#6941c6',
  accentBg: '#f5f1fd',
  green: '#2f9e44',
  red: '#ec4a1e',
  s1: '#2f9e44',
  s2: '#f0a02e',
  s3: '#2f80ed',
};

export const dark: Theme = {
  dark: true,
  bg: '#0e0c14',
  card: '#17141f',
  line: '#2b2438',
  line2: '#221c2e',
  wash: '#1b1626',
  text: '#c5bdd6',
  head: '#f2eefa',
  muted: '#8a819e',
  accent: '#a189f0',
  accentBg: '#241d38',
  green: '#3ecf6a',
  red: '#ff6a4d',
  s1: '#3ecf6a',
  s2: '#f5b942',
  s3: '#5b9cf5',
};

/** Type scale. Raised twice on the web after the owner needed a magnifier;
 *  these are the sizes that survived that, not the original desktop ones. */
export const type = {
  hero: 40,
  big: 22,
  title: 20,
  row: 17.5,
  body: 17,
  label: 14,
  small: 15,
};

export const STRAT: Record<string, { name: string; short: string; key: keyof Theme }> = {
  s1: { name: 'S1 Pullback', short: 'S1', key: 's1' },
  s2: { name: 'S2 Breakout', short: 'S2', key: 's2' },
  s3: { name: 'S3 Leader Dip', short: 's3' as any, key: 's3' },
};
