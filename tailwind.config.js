/**
 * Tailwind config for Yggdrasil 2.0.
 *
 * Compiled with the standalone Tailwind CLI (no node/npm at deploy time) via
 * scripts/build_css.sh -> static/css/tailwind.css, served by WhiteNoise.
 * Colors/fonts map onto the --ygg-* CSS custom properties in
 * static/css/tokens.css, which stays the single source of truth for the palette.
 */
module.exports = {
  content: [
    './templates/**/*.html',
    './*/templates/**/*.html',
    './static/js/nav.js',
  ],
  // `.collapse` is a Bootstrap component class used across legacy templates.
  // Tailwind would otherwise emit a `.collapse{visibility:collapse}` utility
  // that overrides Bootstrap's collapse panels and hides them. Block it.
  blocklist: ['collapse'],
  theme: {
    extend: {
      colors: {
        ink: {
          950: 'var(--ygg-ink-950)',
          900: 'var(--ygg-ink-900)',
          800: 'var(--ygg-ink-800)',
          700: 'var(--ygg-ink-700)',
        },
        navy: {
          600: 'var(--ygg-navy-600)',
          500: 'var(--ygg-navy-500)',
        },
        green: {
          700: 'var(--ygg-green-700)',
          600: 'var(--ygg-green-600)',
          500: 'var(--ygg-green-500)',
          400: 'var(--ygg-green-400)',
          300: 'var(--ygg-green-300)',
          100: 'var(--ygg-green-100)',
        },
        gold: {
          600: 'var(--ygg-gold-600)',
          500: 'var(--ygg-gold-500)',
          400: 'var(--ygg-gold-400)',
          200: 'var(--ygg-gold-200)',
        },
        surface: {
          DEFAULT: 'var(--ygg-surface)',
          alt: 'var(--ygg-surface-alt)',
          sunken: 'var(--ygg-surface-sunken)',
        },
        line: {
          DEFAULT: 'var(--ygg-border)',
          strong: 'var(--ygg-border-strong)',
        },
        content: {
          DEFAULT: 'var(--ygg-text)',
          muted: 'var(--ygg-text-muted)',
          ondark: 'var(--ygg-text-on-dark)',
          ondarkmuted: 'var(--ygg-text-on-dark-muted)',
        },
        primary: {
          DEFAULT: 'var(--ygg-primary)',
          hover: 'var(--ygg-primary-hover)',
          soft: 'var(--ygg-primary-soft)',
        },
        accent: 'var(--ygg-accent)',
        proj: {
          maxillo: 'var(--ygg-maxillo)',
          brain: 'var(--ygg-brain)',
          laparoscopy: 'var(--ygg-laparoscopy)',
        },
        ok: 'var(--ygg-ok)',
        warn: 'var(--ygg-warn)',
        danger: 'var(--ygg-danger)',
      },
      fontFamily: {
        sans: 'var(--ygg-font-body)',
        display: 'var(--ygg-font-display)',
        mono: 'var(--ygg-font-mono)',
      },
      borderRadius: {
        sm: 'var(--ygg-radius-sm)',
        md: 'var(--ygg-radius-md)',
        lg: 'var(--ygg-radius-lg)',
      },
      boxShadow: {
        card: 'var(--ygg-shadow-card-light)',
        pop: 'var(--ygg-shadow-pop)',
        'card-dark': 'var(--ygg-shadow-card)',
      },
      maxWidth: {
        content: '1200px',
      },
    },
  },
  plugins: [],
};
