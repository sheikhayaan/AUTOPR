/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        // "Royal amethyst & gold" — a committed jewel-box dark theme. Deep
        // obsidian-violet canvas, amethyst as the primary, champagne gold as the
        // ceremonial accent, warm parchment ink. Reads as a premium control
        // plane, not another indigo SaaS dashboard.
        canvas: '#0a0711',
        surface: '#130f1e',
        'surface-2': '#1b1529',
        'surface-3': '#251d37',
        // Hairlines carry a faint warm-gold tint — the signature that ties the
        // whole surface language together.
        line: 'rgba(214,183,110,0.10)',
        'line-strong': 'rgba(214,183,110,0.20)',
        ink: '#f2ede4',
        'ink-dim': '#b8aecb',
        'ink-faint': '#7d7593',
        brand: {
          50: '#f5f2ff',
          200: '#ddccff',
          300: '#c4a9ff',
          400: '#a985ff',
          500: '#8b5cf6',
          600: '#7c3aed',
          700: '#6d28d9',
        },
        // Champagne → classic gold. Used for ceremonial accents: the wordmark,
        // hero numerals, the active rail, the royal CTA.
        gold: {
          300: '#f6e6ac',
          400: '#ecd587',
          500: '#d6b76e',
          600: '#c19a47',
          700: '#9c7a34',
        },
        risk: {
          trivial: '#38bdf8',
          low: '#34d399',
          medium: '#fbbf24',
          high: '#fb7185',
        },
      },
      fontFamily: {
        // Editorial serif for display headings — the elegance lever. Paired with
        // Inter for all UI/body text (the classic luxe pairing).
        display: [
          'Fraunces', 'ui-serif', 'Georgia', 'Cambria', 'Times New Roman', 'serif',
        ],
        sans: [
          'Inter var', 'Inter', 'ui-sans-serif', 'system-ui', '-apple-system',
          'Segoe UI', 'Roboto', 'Helvetica Neue', 'Arial', 'sans-serif',
        ],
        mono: [
          'ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas',
          'Liberation Mono', 'monospace',
        ],
      },
      boxShadow: {
        card: '0 1px 0 0 rgba(255,255,255,0.05) inset, 0 18px 44px -22px rgba(0,0,0,0.85)',
        pop: '0 28px 70px -22px rgba(0,0,0,0.8)',
        glow: '0 0 0 1px rgba(139,92,246,0.35), 0 10px 34px -8px rgba(139,92,246,0.5)',
        'glow-gold': '0 0 0 1px rgba(214,183,110,0.4), 0 10px 30px -10px rgba(214,183,110,0.35)',
      },
      backgroundImage: {
        'grid-faint':
          'linear-gradient(rgba(214,183,110,0.03) 1px, transparent 1px), linear-gradient(90deg, rgba(214,183,110,0.03) 1px, transparent 1px)',
        // Layered ambient: amethyst bloom top-left, a whisper of gold top-right.
        'royal-aurora':
          'radial-gradient(90% 80% at 8% -10%, rgba(139,92,246,0.22), transparent 55%), radial-gradient(70% 60% at 100% 0%, rgba(214,183,110,0.10), transparent 50%)',
        'brand-sheen':
          'radial-gradient(120% 120% at 0% 0%, rgba(139,92,246,0.18), transparent 55%)',
        'gold-line':
          'linear-gradient(90deg, transparent, rgba(214,183,110,0.5), transparent)',
        'gold-text':
          'linear-gradient(135deg, #f6e6ac 0%, #d6b76e 45%, #c19a47 100%)',
      },
      keyframes: {
        'fade-up': {
          '0%': { opacity: '0', transform: 'translateY(6px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        'pulse-ring': {
          '0%': { boxShadow: '0 0 0 0 rgba(52,211,153,0.5)' },
          '70%': { boxShadow: '0 0 0 6px rgba(52,211,153,0)' },
          '100%': { boxShadow: '0 0 0 0 rgba(52,211,153,0)' },
        },
      },
      animation: {
        'fade-up': 'fade-up 0.35s cubic-bezier(0.16,1,0.3,1) both',
        'pulse-ring': 'pulse-ring 2s infinite',
      },
    },
  },
  plugins: [],
}
