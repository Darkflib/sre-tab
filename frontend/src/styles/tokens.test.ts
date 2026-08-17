import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

/**
 * Contrast is a property of the token values, so it can be checked without a
 * browser: parse `tokens.css`, recompute the WCAG ratios, and fail if any
 * pair the design depends on has drifted below its threshold.
 *
 * This exists because the dark theme's contrast had never been measured — it
 * had been eyeballed from screenshots, and several boundaries were sitting at
 * around 1.4:1. Numbers in a comment rot; these do not.
 *
 * Thresholds, and why they differ:
 *
 * - 4.5:1 for body text — WCAG 2.2 AA, 1.4.3.
 * - 3:1 for the borders of *interactive* controls — 1.4.11, which asks for
 *   3:1 on "visual information required to identify user interface
 *   components". A button, an input, and an inactive chip are identified by
 *   their edge, so `--border-strong` is held to it.
 * - Container borders (`--border`, on cards and panels) are deliberately not
 *   held to 3:1: 1.4.11 does not cover a decorative container whose identity
 *   comes from its content, and a grid of 3:1 outlines reads as a table.
 *   They are held to a floor of 1.6:1 instead, which is enough to see and
 *   which the pre-fix value of 1.32:1 would have failed.
 */

const CSS = readFileSync(fileURLToPath(new URL('./tokens.css', import.meta.url)), 'utf8');

type Palette = Record<string, string>;

function paletteFrom(source: string): Palette {
  const palette: Palette = {};
  for (const [, name, value] of source.matchAll(/--([a-z0-9-]+):\s*(#[0-9a-f]{6})\b/gi)) {
    palette[name] = value.toLowerCase();
  }
  return palette;
}

function blockBetween(startMarker: string): string {
  const start = CSS.indexOf(startMarker);
  if (start === -1) throw new Error(`could not find ${startMarker} in tokens.css`);
  const open = CSS.indexOf('{', start);
  let depth = 0;
  for (let i = open; i < CSS.length; i += 1) {
    if (CSS[i] === '{') depth += 1;
    else if (CSS[i] === '}') {
      depth -= 1;
      if (depth === 0) return CSS.slice(open + 1, i);
    }
  }
  throw new Error(`unbalanced braces after ${startMarker}`);
}

const light = paletteFrom(blockBetween('\n:root {'));
const dark = paletteFrom(blockBetween("\n:root[data-theme='dark'] {"));
const darkFallback = paletteFrom(blockBetween('\n@media (prefers-color-scheme: dark) {'));

// --- WCAG maths ---------------------------------------------------------

function channel(value: number): number {
  const c = value / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  const n = Number.parseInt(hex.slice(1), 16);
  return (
    0.2126 * channel((n >> 16) & 0xff) +
    0.7152 * channel((n >> 8) & 0xff) +
    0.0722 * channel(n & 0xff)
  );
}

export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** Composite `fg` over `bg` at `alpha` — for tokens used with `opacity`. */
function flatten(fg: string, bg: string, alpha: number): string {
  const parse = (hex: string) => {
    const n = Number.parseInt(hex.slice(1), 16);
    return [(n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff];
  };
  const [fr, fg_, fb] = parse(fg);
  const [br, bg_, bb] = parse(bg);
  const mix = (f: number, b: number) => Math.round(f * alpha + b * (1 - alpha));
  return (
    '#' +
    [mix(fr, br), mix(fg_, bg_), mix(fb, bb)].map((v) => v.toString(16).padStart(2, '0')).join('')
  );
}

// --- what is checked ----------------------------------------------------

/** [description, foreground token, background token, minimum ratio] */
type Pair = [string, string, string, number];

const TEXT: Pair[] = [
  ['body text on the page', 'text', 'bg', 4.5],
  ['body text on a card', 'text', 'surface', 4.5],
  ['body text on a read card', 'text', 'surface-2', 4.5],
  ['body text on a hovered surface', 'text', 'surface-hover', 4.5],
  ['muted text on the page', 'text-muted', 'bg', 4.5],
  ['muted text on a card', 'text-muted', 'surface', 4.5],
  ['muted text on a read card / inactive chip', 'text-muted', 'surface-2', 4.5],
  ['muted text on a hovered surface', 'text-muted', 'surface-hover', 4.5],
  ['link on a card', 'accent', 'surface', 4.5],
  ['link on the page', 'accent', 'bg', 4.5],
  ['active nav link on the header', 'accent', 'accent-soft', 4.5],
  ['primary button label', 'accent-contrast', 'accent', 4.5],
  ['primary button label, hovered', 'accent-contrast', 'accent-hover', 4.5],
  ['active chip label', 'accent-contrast', 'accent', 4.5],
  ['danger button label', 'danger', 'surface', 4.5],
  ['danger button label, hovered', 'danger-contrast', 'danger', 4.5],
  ['danger text in the danger zone', 'danger', 'error-bg', 4.5],
  ['dominance notice text', 'text', 'notice-bg', 4.5],
  ['high-volume flag text', 'text', 'notice-bg', 4.5],
  ['error banner text', 'text', 'error-bg', 4.5],
  ['text on a selected option', 'text', 'accent-soft', 4.5],
];

const INTERACTIVE_BOUNDARIES: Pair[] = [
  ['button/input border on a card', 'border-strong', 'surface', 3],
  ['button/input border on the page', 'border-strong', 'bg', 3],
  ['inactive chip border against its own fill', 'border-strong', 'surface-2', 3],
  ['active chip fill against the filter panel', 'accent', 'surface', 3],
  ['selected option border', 'accent', 'surface', 3],
  ['danger button border', 'danger', 'surface', 3],
  ['dominance notice border on the page', 'notice-border', 'bg', 3],
  ['error banner border on the page', 'error-border', 'bg', 3],
  ['composition bar fill against its track', 'accent', 'surface-2', 3],
];

const FOCUS: Pair[] = [
  ['focus ring on the page', 'focus', 'bg', 3],
  ['focus ring on a card', 'focus', 'surface', 3],
  ['focus ring on a read card', 'focus', 'surface-2', 3],
  ['focus ring on a selected option', 'focus', 'accent-soft', 3],
  ['focus ring on the notice banner', 'focus', 'notice-bg', 3],
  ['focus ring on the error banner', 'focus', 'error-bg', 3],
  // The halo fills the outline-offset gap, so this is the pair that decides
  // whether the ring reads as a ring on an accent- or danger-filled control.
  ['focus ring against its own halo', 'focus', 'focus-halo', 3],
];

/** Container edges: visible, but deliberately below the 3:1 UI threshold. */
const CONTAINER_BOUNDARIES: Pair[] = [
  ['card border on the page', 'border', 'bg', 1.6],
  ['card border against the card fill', 'border', 'surface', 1.6],
  ['tag border on a card', 'border', 'surface', 1.6],
];

const THEMES: [string, Palette][] = [
  ['light', light],
  ['dark', dark],
];

describe('tokens.css', () => {
  it('parses a palette for each theme', () => {
    expect(Object.keys(light).length).toBeGreaterThan(15);
    expect(Object.keys(dark).length).toBeGreaterThan(15);
  });

  it('defines the dark palette identically in the media query and the attribute selector', () => {
    // The file states the dark values twice: once for `data-theme='dark'`
    // and once as the JavaScript-off fallback. Nothing but this test stops
    // one copy being edited and the other forgotten, which would show up
    // only for users who block scripts.
    expect(darkFallback).toEqual(dark);
  });

  it.each(THEMES)('%s: every token the tests reference exists', (_name, palette) => {
    const referenced = new Set(
      [...TEXT, ...INTERACTIVE_BOUNDARIES, ...FOCUS, ...CONTAINER_BOUNDARIES].flatMap(
        ([, fg, bg]) => [fg, bg],
      ),
    );
    for (const token of referenced) {
      expect(palette, `--${token} is missing`).toHaveProperty(token);
    }
  });
});

describe.each(THEMES)('%s theme contrast', (_name, palette) => {
  it.each(TEXT)('%s clears AA body text (4.5:1)', (_label, fg, bg, min) => {
    expect(contrast(palette[fg], palette[bg])).toBeGreaterThanOrEqual(min);
  });

  it.each(INTERACTIVE_BOUNDARIES)('%s clears 1.4.11 (3:1)', (_label, fg, bg, min) => {
    expect(contrast(palette[fg], palette[bg])).toBeGreaterThanOrEqual(min);
  });

  it.each(FOCUS)('%s clears 3:1', (_label, fg, bg, min) => {
    expect(contrast(palette[fg], palette[bg])).toBeGreaterThanOrEqual(min);
  });

  it.each(CONTAINER_BOUNDARIES)('%s stays visible (1.6:1 floor)', (_label, fg, bg, min) => {
    expect(contrast(palette[fg], palette[bg])).toBeGreaterThanOrEqual(min);
  });

  it('keeps the dimmed title on a read card readable', () => {
    // app.css: `.card[data-read='true'] .card__title { opacity: 0.72 }`.
    // The summary is deliberately no longer dimmed — muted text at 0.72 on
    // `--surface-2` came to 3.22:1 in light, which is what this guards.
    const dimmed = flatten(palette.text, palette['surface-2'], 0.72);
    expect(contrast(dimmed, palette['surface-2'])).toBeGreaterThanOrEqual(4.5);
  });

  it('keeps a disabled button label readable', () => {
    // base.css: `.button:disabled { opacity: 0.6 }`. WCAG exempts disabled
    // controls; this holds the line anyway.
    const dimmed = flatten(palette.text, palette.surface, 0.6);
    expect(contrast(dimmed, palette.surface)).toBeGreaterThanOrEqual(4.5);
  });

  it('gives the focus halo enough contrast against accent and danger fills', () => {
    // The halo is what separates the ring from a control filled with the
    // ring's own colour — in dark, `--focus` and `--accent` are identical.
    expect(contrast(palette['focus-halo'], palette.accent)).toBeGreaterThanOrEqual(3);
    expect(contrast(palette['focus-halo'], palette.danger)).toBeGreaterThanOrEqual(3);
  });
});
