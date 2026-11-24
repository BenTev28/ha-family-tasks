// Lints the card (a single dependency-free browser file, no build step).
// Run: npx -y -p eslint@9.39.5 -p globals@16.5.0 eslint
// (CI and scripts_test.sh do this; there is deliberately no package.json.)
import globals from "globals";

export default [
  {
    files: ["custom_components/family_tasks/frontend/**/*.js"],
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: "module",
      globals: { ...globals.browser },
    },
    rules: {
      "no-undef": "error",
      "no-unused-vars": ["error", { caughtErrors: "none" }],
      "no-redeclare": "error",
      "no-dupe-keys": "error",
      "no-unreachable": "error",
    },
  },
];
