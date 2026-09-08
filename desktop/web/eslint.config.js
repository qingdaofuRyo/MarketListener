import js from "@eslint/js";
import ts from "typescript-eslint";
import vue from "eslint-plugin-vue";
import globals from "globals";

export default ts.config(
  { ignores: ["node_modules/**", "test-results/**"] },
  {
    files: ["**/*.{ts,vue}"],
    extends: [js.configs.recommended, ...ts.configs.recommended, ...vue.configs["flat/essential"]],
    languageOptions: { globals: globals.browser, parserOptions: { parser: ts.parser } },
    // Omitted runtime-only fields are intentionally excluded from persisted rest objects.
    rules: { "@typescript-eslint/no-unused-vars": ["error", { ignoreRestSiblings: true }] },
  },
  { files: ["e2e/**/*.ts"], languageOptions: { globals: globals.node } },
);
