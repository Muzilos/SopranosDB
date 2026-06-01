import { defineConfig } from "vite";

// Static bundle for any host / any sub-path: relative asset URLs (`base: "./"`).
// The app is now a thin client over the D1-backed query Worker — there's no
// SQLite WASM to bundle anymore — so this is a plain Vite build.
export default defineConfig({
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "es2020",
  },
});
