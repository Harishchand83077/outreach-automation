import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

const here = dirname(fileURLToPath(import.meta.url));

// `public/` holds the manifest and the icons, so a build is a directory Chrome can load
// unpacked with no further assembly.
//
// **Three entries that share no module, deliberately.** A content script is not an ES module:
// Chrome loads `collect.js` by path and an `import "./shared.js"` inside it would fail at the
// moment it is needed rather than at build time. Rollup only emits a shared chunk for a module
// two entries both import, so the rule that keeps this safe is a source rule -- the panel talks
// to the API and renders, and the scanner and the mappers are imported by `collect.ts` alone.
// `tests/build.test.ts` asserts the built directory has no chunk beyond the three entries.
export default defineConfig({
  root: here,
  publicDir: resolve(here, "public"),
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "chrome114",
    rollupOptions: {
      input: {
        panel: resolve(here, "panel.html"),
        background: resolve(here, "src/background.ts"),
        collect: resolve(here, "src/collect.ts"),
      },
      output: {
        entryFileNames: "[name].js",
        chunkFileNames: "chunk-[name].js",
        assetFileNames: "[name].[ext]",
      },
    },
  },
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.ts"],
  },
});
