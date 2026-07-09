import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // Static assets served as-is (svg/etc. never matched eslint's default
    // .js/.ts extension filter, so this never mattered before; Task 3 added
    // public/alphatab/alphaTab.min.js — a vendored, already-minified
    // third-party build — which does, and lints as ~3000 meaningless
    // warnings otherwise since it's plain JS as far as eslint can tell).
    "public/**",
  ]),
]);

export default eslintConfig;
