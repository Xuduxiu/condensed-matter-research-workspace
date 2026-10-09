import js from '@eslint/js';
import globals from 'globals';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['dist', 'src/i18n.ts', 'src/pages/Compare.tsx', 'src/pages/ConceptDetail.tsx', 'src/pages/Cooccurrence.tsx', 'src/pages/Heatmap.tsx', 'src/pages/Lifecycle.tsx', 'src/pages/Overview.tsx', 'src/pages/Papers.tsx', 'src/pages/ResearchRadar.tsx', 'src/pages/SystemData.tsx', 'src/components/HeatmapChart.tsx', 'src/components/Layout.tsx', 'src/components/StatCard.tsx', 'src/components/TrendLine.tsx'] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ['**/*.{ts,tsx}'],
    languageOptions: { ecmaVersion: 2020, globals: globals.browser },
    plugins: { 'react-hooks': reactHooks, 'react-refresh': reactRefresh },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': 'off',
      '@typescript-eslint/no-explicit-any': 'off',
    },
  },
);
