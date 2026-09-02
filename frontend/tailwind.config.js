/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      // A light, blue-and-white scheme. The scale keeps its original names and
      // direction — 900 is the page, 100 is the strongest text — so every component
      // written against these tokens flipped from dark to light without being touched.
      colors: {
        ink: {
          900: '#FFFFFF', // page
          800: '#FFFFFF', // cards
          700: '#E8EDF5', // hairlines and fills
          600: '#D3DCE8', // borders
          500: '#A9B6C8',
          400: '#71809A', // muted text
          300: '#51607A', // secondary text
          200: '#2D3A50',
          100: '#0E1A2C', // primary text
        },
        signal: {
          DEFAULT: '#1D6FE0',
          soft: '#EFF5FE',
          deep: '#1557B0',
        },
        calm: '#5B7FA8',
      },
      fontFamily: {
        sans: ['Bricolage Grotesque', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'ui-monospace', 'monospace'],
      },
      keyframes: {
        pulseRing: {
          '0%': { transform: 'scale(1)', opacity: '0.55' },
          '100%': { transform: 'scale(1.7)', opacity: '0' },
        },
        rise: {
          '0%': { opacity: '0', transform: 'translateY(6px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
      },
      animation: {
        'pulse-ring': 'pulseRing 1.6s ease-out infinite',
        rise: 'rise .28s ease-out both',
      },
    },
  },
  plugins: [],
}
