import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * Vite configuration.
 *
 * The build splits the React app into a small set of named vendor
 * chunks plus the application code. The motivation: a 1.3 MB single
 * bundle means every page paid for libraries it didn't use (the
 * graph view loaded the chart library, the form view loaded React
 * Flow, etc). With code-splitting the browser only fetches what a
 * given page needs, and shared chunks stay cached across navigations.
 *
 * The chunks are split by usage surface:
 *   - react-vendor: react + react-dom + react-router (every page)
 *   - graph-vendor: @xyflow/react (graph views only)
 *   - charts-vendor: @visx/* + d3-* (analytics views only)
 *   - markdown-vendor: react-markdown + rehype/remark (display blocks)
 *   - form-vendor: react-hook-form + zod + @hookform/resolvers (form fill)
 *   - query-vendor: @tanstack/react-query (every page that fetches)
 *   - motion-vendor: framer-motion (transitions)
 *
 * Anything not matched falls into the default chunk (the app code).
 * The `manualChunks` function is called once per resolved module
 * during the rollup build; returning the same string for related
 * modules groups them into a chunk named after that string.
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id: string): string | undefined {
          // Only chunk node_modules content — keep app code in the
          // default entry chunk so route-level lazy imports stay
          // close to where they're used.
          if (!id.includes("node_modules")) return undefined;
          if (
            id.includes("/react-router") ||
            id.includes("/react-dom/") ||
            id.includes("/react/") ||
            id.includes("/scheduler/")
          ) {
            return "react-vendor";
          }
          if (id.includes("/@xyflow/")) return "graph-vendor";
          if (
            id.includes("/@visx/") ||
            id.includes("/d3-array/") ||
            id.includes("/d3-time-format/") ||
            id.includes("/d3-time/")
          ) {
            return "charts-vendor";
          }
          if (
            id.includes("/react-markdown/") ||
            id.includes("/rehype-raw/") ||
            id.includes("/remark-gfm/") ||
            id.includes("/remark-parse/") ||
            id.includes("/remark-rehype/") ||
            id.includes("/micromark") ||
            id.includes("/mdast-")
          ) {
            return "markdown-vendor";
          }
          if (
            id.includes("/react-hook-form/") ||
            id.includes("/@hookform/") ||
            id.includes("/zod/")
          ) {
            return "form-vendor";
          }
          if (id.includes("/@tanstack/")) return "query-vendor";
          if (id.includes("/framer-motion/")) return "motion-vendor";
          if (id.includes("/prism-react-renderer/")) return "source-vendor";
          return undefined;
        },
      },
    },
    // The default 500 kB warning is too aggressive once we've split
    // — react-vendor + graph-vendor will both legitimately exceed it.
    // Bump to 600 kB; anything growing past that needs investigation,
    // not a config bump.
    chunkSizeWarningLimit: 600,
  },
});
