/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        // A committed, refined dark palette — this is a developer control
        // plane, so it reads as a premium dark tool by design.
        canvas: '#080a10',
        surface: '#0f131c',
        'surface-2': '#151b26',
        'surface-3': '#1c2431',
        line: 'rgba(255,255,255,0.07)',
        'line-strong': 'rgba(255,255,255,0.13)',
        ink: '#e7ecf3',
        'ink-dim': '#9aa7b8',
        'ink-faint': '#5f6b7c',
        brand: {
          50: '#eef1ff',
          200: '#c7cdff',
          300: '#a5abff',
          400: '#8b93ff',
          500: '#6366f1',
          600: '#5457e6',
          700: '#4145c4',
        },
        risk: {
          trivial: '#38bdf8',
          low: '#34d399',
          medium: '#fbbf24',
          high: '#fb7185',
        },
      },
      fontFamily: {
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
        card: '0 1px 0 0 rgba(255,255,255,0.04) inset, 0 12px 32px -18px rgba(0,0,0,0.7)',
        pop: '0 24px 60px -20px rgba(0,0,0,0.75)',
        glow: '0 0 0 1px rgba(99,102,241,0.35), 0 8px 30px -8px rgba(99,102,241,0.45)',
      },
      backgroundImage: {
        'grid-faint':
          'linear-gradient(rgba(255,255,255,0.025) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.025) 1px, transparent 1px)',
        'brand-sheen':
          'radial-gradient(120% 120% at 0% 0%, rgba(99,102,241,0.16), transparent 55%)',
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
