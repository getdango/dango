/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./dango/web/templates/**/*.html",
    "./dango/web/static/js/**/*.js",
  ],
  safelist: ['xl:table-cell'],
  theme: {
    extend: {
      colors: {
        dango: {
          50: '#fef2f2',
          100: '#fee2e2',
          200: '#fecaca',
          300: '#fca5a5',
          400: '#f87171',
          500: '#ef4444',
          600: '#dc2626',
          700: '#b91c1c',
          800: '#991b1b',
          900: '#7f1d1d',
        },
      },
    },
  },
  plugins: [],
}
