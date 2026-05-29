import { defineConfig } from "vite";

// Static bundle for any host / any sub-path: relative asset URLs (`base: "./"`).
// sqlite-wasm-http spawns ES-module Web Workers and ships the official SQLite
// WASM as an asset — Vite bundles both automatically (`worker.format: "es"`).
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
  // sqlite-wasm-http must not be pre-bundled (it has worker/wasm entry points
  // Vite's optimizer would mangle).
  optimizeDeps: {
    exclude: ["sqlite-wasm-http"],
  },
});
