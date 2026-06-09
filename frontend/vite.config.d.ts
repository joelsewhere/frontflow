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
declare const _default: import("vite").UserConfig;
export default _default;
