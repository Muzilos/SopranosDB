import { defineConfig } from "vite";

// Static bundle for any host / any sub-path: relative asset URLs (`base: "./"`).
// @sqlite.org/sqlite-wasm ships the official SQLite WASM as an asset; Vite emits
// it and rewrites the `new URL(..., import.meta.url)` references.
export default defineConfig({
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "es2020",
    assetsInlineLimit: 0, // never inline the .wasm
  },
  worker: {
    format: "es",
  },
  // The SQLite WASM package must not be pre-bundled (it references its .wasm via
  // import.meta.url, which Vite's optimizer would mangle).
  optimizeDeps: {
    exclude: ["@sqlite.org/sqlite-wasm"],
  },
});
