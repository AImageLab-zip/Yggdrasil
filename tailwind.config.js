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
  // Sitewide dark mode is driven by `data-theme="dark"` on <html> (set pre-paint
  // in base.html, toggled in nav.js). Most surfaces flip for free because the
  // semantic colors below resolve --ygg-* vars re-pointed in tokens.css; this
  // selector additionally enables explicit `dark:` variant utilities.
  darkMode: ['selector', '[data-theme="dark"]'],
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
        blue: {
          600: 'var(--ygg-blue-600)',
          500: 'var(--ygg-blue-500)',
          400: 'var(--ygg-blue-400)',
          100: 'var(--ygg-blue-100)',
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
          2: 'var(--ygg-surface-2)',
          3: 'var(--ygg-surface-3)',
        },
        line: {
          DEFAULT: 'var(--ygg-border)',
          strong: 'var(--ygg-border-strong)',
        },
        content: {
          DEFAULT: 'var(--ygg-text)',
          muted: 'var(--ygg-text-muted)',
          faint: 'var(--ygg-text-faint)',
          ondark: 'var(--ygg-text-on-dark)',
          ondarkmuted: 'var(--ygg-text-on-dark-muted)',
        },
        primary: {
          DEFAULT: 'var(--ygg-primary)',
          hover: 'var(--ygg-primary-hover)',
          soft: 'var(--ygg-primary-soft)',
        },
        // Text/icon color that sits ON a primary fill (flips to ink on dark).
        onprimary: 'var(--ygg-on-primary)',
        // Green — life / success. Distinct from `ok`, which is the status color.
        accent2: {
          DEFAULT: 'var(--ygg-accent2)',
          hover: 'var(--ygg-accent2-hover)',
          soft: 'var(--ygg-accent2-soft)',
        },
        // Gold. Landing runic wordmark ONLY — not for UI chrome.
        accent: {
          DEFAULT: 'var(--ygg-accent)',
          soft: 'var(--ygg-accent-soft)',
        },
        proj: {
          maxillo: 'var(--ygg-maxillo)',
          brain: 'var(--ygg-brain)',
          laparoscopy: 'var(--ygg-laparoscopy)',
        },
        ok: { DEFAULT: 'var(--ygg-ok)', soft: 'var(--ygg-ok-soft)' },
        warn: { DEFAULT: 'var(--ygg-warn)', soft: 'var(--ygg-warn-soft)' },
        danger: { DEFAULT: 'var(--ygg-danger)', soft: 'var(--ygg-danger-soft)' },
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
