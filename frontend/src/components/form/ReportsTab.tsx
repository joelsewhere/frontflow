import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import {
  getAnalyticsState,
  getAnalyticsCurrentStep,
  getAnalyticsStepCounts,
  getAnalyticsFlow,
  getAnalyticsCompletionTime,
  getAnalyticsStepTime,
  getAnalyticsStepHistogram,
  getAnalyticsThroughput,
  getAnalyticsSubmissionRate,
  getAnalyticsDefaults,
  type AnalyticsBucket,
  type AnalyticsFilters,
  type AnalyticsResponse,
  type CompletionTimeResponse,
  type FlowResponse,
  type StepHistogramResponse,
  type StepTimeResponse,
  type SubmissionRateInterval,
  type SubmissionRateResponse,
  type ThroughputInterval,
  type ThroughputResponse,
} from "../../lib/api";
import { FlowSankey } from "./FlowSankey";
import { HistogramChart } from "./HistogramChart";
import { StepTimeChart, formatDuration } from "./StepTimeChart";
import { SubmissionRateChart } from "./SubmissionRateChart";
import { ThroughputChart } from "./ThroughputChart";

interface ReportsTabProps {
  formId: string;
}

// The set of known date-range presets the server accepts. Kept in
// sync with `_DATE_RANGE_PRESETS` on the backend.
const DATE_RANGE_OPTIONS: {
  value: NonNullable<AnalyticsFilters["date_range"]>;
  label: string;
}[] = [
  { value: "last_7_days", label: "Last 7 days" },
  { value: "last_30_days", label: "Last 30 days" },
  { value: "last_90_days", label: "Last 90 days" },
  { value: "all_time", label: "All time" },
];

/** URL-param-backed filter state for the Reports tab. Drives every
 *  chart on the page. Clicking a bar toggles that filter on; the
 *  rest of the page (other charts) re-narrows to the filtered set.
 *  Charts always render every bucket — non-matching bars grey out
 *  rather than vanish, so the axis stays stable as the user clicks
 *  around.
 *
 *  Date range is one-or-the-other: either a named preset (rolling,
 *  recomputed against `now` on each request) or an explicit
 *  start/end pair (frozen). They never coexist — picking a preset
 *  clears any custom dates and vice versa. */
function useReportsFilters(): {
  filters: AnalyticsFilters;
  toggleState: (s: string) => void;
  toggleCurrentStep: (s: string) => void;
  setDateRange: (
    r: NonNullable<AnalyticsFilters["date_range"]>,
  ) => void;
  setCustomDateRange: (start: string, end: string) => void;
  resetToDefaults: (defaults: {
    state: string[] | null;
    current_step: string[] | null;
    date_range: string | null;
  }) => void;
} {
  const [params, setParams] = useSearchParams();

  const filters: AnalyticsFilters = useMemo(() => {
    const state = params.getAll("state");
    const current_step = params.getAll("current_step");
    const dr = params.get("date_range") as
      | AnalyticsFilters["date_range"]
      | null;
    const start_date = params.get("start_date");
    const end_date = params.get("end_date");
    return {
      ...(state.length > 0 ? { state } : {}),
      ...(current_step.length > 0 ? { current_step } : {}),
      // Explicit dates win — when both are in the URL, drop the
      // preset (this can only happen if a hand-crafted URL has
      // both; the picker keeps them mutually exclusive).
      ...((start_date || end_date)
        ? {
            ...(start_date ? { start_date } : {}),
            ...(end_date ? { end_date } : {}),
          }
        : dr
          ? { date_range: dr }
          : {}),
    };
  }, [params]);

  const update = useCallback(
    (mutate: (p: URLSearchParams) => void) => {
      const next = new URLSearchParams(params);
      mutate(next);
      setParams(next, { replace: false });
    },
    [params, setParams],
  );

  const toggleListParam = useCallback(
    (key: string, value: string) =>
      update((p) => {
        const current = p.getAll(key);
        p.delete(key);
        if (current.includes(value)) {
          for (const v of current) if (v !== value) p.append(key, v);
        } else {
          for (const v of current) p.append(key, v);
          p.append(key, value);
        }
      }),
    [update],
  );

  return {
    filters,
    toggleState: (s) => toggleListParam("state", s),
    toggleCurrentStep: (s) => toggleListParam("current_step", s),
    setDateRange: (r) =>
      update((p) => {
        // Picking a preset clears any custom-range params — the two
        // modes are mutually exclusive. We always write the preset
        // to the URL; the URL is the single source of truth, so the
        // "current selection" is always visible there even when it
        // matches the framework default.
        p.delete("start_date");
        p.delete("end_date");
        p.set("date_range", r);
      }),
    setCustomDateRange: (start, end) =>
      update((p) => {
        // Custom dates clear any preset, and vice versa.
        p.delete("date_range");
        if (start) p.set("start_date", start);
        else p.delete("start_date");
        if (end) p.set("end_date", end);
        else p.delete("end_date");
      }),
    /** Reset URL to the form's resolved defaults. The defaults come
     *  from the server (`/analytics/defaults`) so authors who set
     *  `@form(reports=...)` overrides get their values respected. */
    resetToDefaults: (defaults: {
      state: string[] | null;
      current_step: string[] | null;
      date_range: string | null;
    }) =>
      update((p) => {
        // Wipe everything first.
        p.delete("state");
        p.delete("current_step");
        p.delete("date_range");
        p.delete("start_date");
        p.delete("end_date");
        if (defaults.state) {
          for (const s of defaults.state) p.append("state", s);
        }
        if (defaults.current_step) {
          for (const s of defaults.current_step) p.append("current_step", s);
        }
        if (defaults.date_range) {
          p.set("date_range", defaults.date_range);
        }
      }),
  };
}

/** URL-backed state for chart-local controls (scale toggles,
 *  drilldown selection, interval pickers). Same URL-as-truth model
 *  as the data filters in `useReportsFilters`, but conceptually
 *  separate: filters narrow *what data* the charts show; these
 *  control *how* the charts present that data.
 *
 *  Default-omission: each control omits its URL param when at the
 *  default value, so a bare URL means "everything at default" and
 *  the URL stays minimal in the common case. */
function useChartControls(): {
  stepTimeScale: "shared" | "per-step";
  setStepTimeScale: (m: "shared" | "per-step") => void;
  throughputInterval: ThroughputInterval | null;
  setThroughputInterval: (i: ThroughputInterval | null) => void;
  submissionRateInterval: SubmissionRateInterval | null;
  setSubmissionRateInterval: (i: SubmissionRateInterval | null) => void;
  stepTimeDrilldown: string | null;
  setStepTimeDrilldown: (nodeId: string | null) => void;
} {
  const [params, setParams] = useSearchParams();

  const stepTimeScale =
    (params.get("step_time_scale") as "shared" | "per-step" | null) === "per-step"
      ? "per-step"
      : "shared";
  const throughputInterval = (() => {
    const v = params.get("throughput_interval");
    return v === "day" || v === "week" || v === "month" ? v : null;
  })();
  const submissionRateInterval = (() => {
    const v = params.get("submission_rate_interval");
    if (
      v === "minute" || v === "5min" || v === "15min" ||
      v === "hour" || v === "day" || v === "week" || v === "month"
    ) {
      return v;
    }
    return null;
  })();
  const stepTimeDrilldown = params.get("step_time_drilldown");

  const update = useCallback(
    (mutate: (p: URLSearchParams) => void) => {
      const next = new URLSearchParams(params);
      mutate(next);
      setParams(next, { replace: false });
    },
    [params, setParams],
  );

  return {
    stepTimeScale,
    setStepTimeScale: (m) =>
      update((p) => {
        // Default `shared` omitted from URL.
        if (m === "shared") p.delete("step_time_scale");
        else p.set("step_time_scale", m);
      }),
    throughputInterval,
    setThroughputInterval: (i) =>
      update((p) => {
        // Default auto (null) omitted from URL.
        if (i === null) p.delete("throughput_interval");
        else p.set("throughput_interval", i);
      }),
    submissionRateInterval,
    setSubmissionRateInterval: (i) =>
      update((p) => {
        if (i === null) p.delete("submission_rate_interval");
        else p.set("submission_rate_interval", i);
      }),
    stepTimeDrilldown,
    setStepTimeDrilldown: (nodeId) =>
      update((p) => {
        if (nodeId === null) p.delete("step_time_drilldown");
        else p.set("step_time_drilldown", nodeId);
      }),
  };
}

/** Stable React Query key for an analytics chart. Including the
 *  serialized filter shape means a filter toggle invalidates the
 *  right chart's cache without bleeding into the others. */
function analyticsKey(
  chart: string,
  formId: string,
  filters: AnalyticsFilters,
): unknown[] {
  return ["analytics", chart, formId, filters];
}

function useAnalyticsState(formId: string, filters: AnalyticsFilters) {
  return useQuery({
    queryKey: analyticsKey("state", formId, filters),
    queryFn: () => getAnalyticsState(formId, filters),
  });
}

function useAnalyticsCurrentStep(
  formId: string,
  filters: AnalyticsFilters,
) {
  return useQuery({
    queryKey: analyticsKey("current_step", formId, filters),
    queryFn: () => getAnalyticsCurrentStep(formId, filters),
  });
}

function useAnalyticsStepCounts(
  formId: string,
  filters: AnalyticsFilters,
) {
  return useQuery({
    queryKey: analyticsKey("step_counts", formId, filters),
    queryFn: () => getAnalyticsStepCounts(formId, filters),
  });
}

function useAnalyticsFlow(formId: string, filters: AnalyticsFilters) {
  return useQuery({
    queryKey: analyticsKey("flow", formId, filters),
    queryFn: () => getAnalyticsFlow(formId, filters),
  });
}

function useAnalyticsCompletionTime(
  formId: string,
  filters: AnalyticsFilters,
) {
  return useQuery({
    queryKey: analyticsKey("completion_time", formId, filters),
    queryFn: () => getAnalyticsCompletionTime(formId, filters),
  });
}

function useAnalyticsStepTime(
  formId: string,
  filters: AnalyticsFilters,
) {
  return useQuery({
    queryKey: analyticsKey("step_time", formId, filters),
    queryFn: () => getAnalyticsStepTime(formId, filters),
  });
}

function useAnalyticsStepHistogram(
  formId: string,
  nodeId: string | null,
  filters: AnalyticsFilters,
) {
  return useQuery({
    queryKey: analyticsKey(`step_time/${nodeId ?? ""}`, formId, filters),
    queryFn: () => getAnalyticsStepHistogram(formId, nodeId!, filters),
    enabled: !!nodeId,
  });
}

function useAnalyticsThroughput(
  formId: string,
  filters: AnalyticsFilters,
  interval: ThroughputInterval | null,
) {
  return useQuery({
    queryKey: analyticsKey(
      `throughput:${interval ?? "auto"}`,
      formId,
      filters,
    ),
    queryFn: () =>
      getAnalyticsThroughput(formId, filters, interval ?? undefined),
  });
}

function useAnalyticsSubmissionRate(
  formId: string,
  filters: AnalyticsFilters,
  interval: SubmissionRateInterval | null,
) {
  return useQuery({
    queryKey: analyticsKey(
      `submission_rate:${interval ?? "auto"}`,
      formId,
      filters,
    ),
    queryFn: () =>
      getAnalyticsSubmissionRate(formId, filters, interval ?? undefined),
  });
}

export function ReportsTab({ formId }: ReportsTabProps) {
  const {
    filters,
    toggleState,
    toggleCurrentStep,
    setDateRange,
    setCustomDateRange,
    resetToDefaults,
  } = useReportsFilters();

  // Fetch the form's resolved default filters. The Reports tab uses
  // these to seed URL query params on first mount so the URL is
  // always the source of truth for which filters are active —
  // there's no "implicit default" state that the user can't see.
  const defaultsQ = useQuery({
    queryKey: ["analytics", "defaults", formId],
    queryFn: () => getAnalyticsDefaults(formId),
    staleTime: 60_000, // defaults don't change often
  });

  // Seed-on-mount: when defaults arrive AND the URL is bare (no
  // analytics-related query params), write the defaults into the
  // URL. After this runs once, every subsequent state change is
  // user-driven and visible in the URL. The `seeded` ref prevents
  // re-seeding after the user explicitly clears filters (which
  // sets the URL to a bare state — we don't want to immediately
  // overwrite that with defaults again).
  const seededRef = useRef(false);
  useEffect(() => {
    if (seededRef.current) return;
    if (!defaultsQ.data) return;
    const urlHasAny =
      !!filters.state ||
      !!filters.current_step ||
      !!filters.date_range ||
      !!filters.start_date ||
      !!filters.end_date;
    if (!urlHasAny) {
      resetToDefaults(defaultsQ.data);
    }
    seededRef.current = true;
  }, [defaultsQ.data, filters, resetToDefaults]);

  const stateQ = useAnalyticsState(formId, filters);
  const currentStepQ = useAnalyticsCurrentStep(formId, filters);
  const stepCountsQ = useAnalyticsStepCounts(formId, filters);
  const flowQ = useAnalyticsFlow(formId, filters);
  const completionQ = useAnalyticsCompletionTime(formId, filters);
  const stepTimeQ = useAnalyticsStepTime(formId, filters);
  const throughputQ = useAnalyticsThroughput(
    formId, filters, /*interval=*/ null,
  );
  // Chart-local controls — same URL-as-truth model as the filters.
  const {
    stepTimeScale, setStepTimeScale,
    throughputInterval, setThroughputInterval,
    submissionRateInterval, setSubmissionRateInterval,
    stepTimeDrilldown: expandedStep, setStepTimeDrilldown: setExpandedStep,
  } = useChartControls();

  const stepHistogramQ = useAnalyticsStepHistogram(
    formId, expandedStep, filters,
  );
  const throughputOverrideQ = useAnalyticsThroughput(
    formId, filters, throughputInterval,
  );
  // Pick whichever is most relevant — the override result if the
  // user set one, otherwise the auto one. Both queries run; this
  // wastes one round-trip when the override is set, acceptable.
  const activeThroughputQ = throughputInterval
    ? throughputOverrideQ
    : throughputQ;

  const submissionRateQ = useAnalyticsSubmissionRate(
    formId, filters, /*interval=*/ null,
  );
  const submissionRateOverrideQ = useAnalyticsSubmissionRate(
    formId, filters, submissionRateInterval,
  );
  const activeSubmissionRateQ = submissionRateInterval
    ? submissionRateOverrideQ
    : submissionRateQ;

  // "Differs from defaults" — drives whether the Reset button shows.
  // We compare the URL-derived filter to the server-resolved defaults
  // (both lists are compared as sorted sets; date_range is direct).
  const differsFromDefaults = useMemo(() => {
    if (!defaultsQ.data) return false;
    const setEq = (a: string[] | undefined, b: string[] | null) => {
      const aa = [...(a ?? [])].sort();
      const bb = [...(b ?? [])].sort();
      if (aa.length !== bb.length) return false;
      return aa.every((v, i) => v === bb[i]);
    };
    if (!setEq(filters.state, defaultsQ.data.state)) return true;
    if (!setEq(filters.current_step, defaultsQ.data.current_step)) return true;
    if ((filters.date_range ?? null) !== defaultsQ.data.date_range) return true;
    if (filters.start_date || filters.end_date) return true;
    return false;
  }, [filters, defaultsQ.data]);

  return (
    <div className="flex flex-col gap-8">
      {/* Filter bar — the URL is the source of truth for what's
          filtered. The chart bars themselves visualize which states
          / steps are selected (active vs greyed), so we don't
          duplicate that with filter-pills here; only the date range
          and a reset-to-defaults button live in this bar. */}
      <div className="flex flex-col gap-3 border border-border bg-surface px-4 py-3">
        <div className="flex flex-wrap items-center gap-4">
          <DateRangeControl
            preset={filters.date_range ?? null}
            startDate={filters.start_date ?? null}
            endDate={filters.end_date ?? null}
            onPickPreset={setDateRange}
            onApplyCustom={setCustomDateRange}
          />
          {differsFromDefaults ? (
            <button
              type="button"
              onClick={() =>
                defaultsQ.data && resetToDefaults(defaultsQ.data)
              }
              className="font-sans text-xs uppercase tracking-[0.16em] text-muted hover:text-ink"
            >
              Reset filters
            </button>
          ) : null}
        </div>
      </div>

      {/* Charts. Each is independently loaded so a slow one doesn't
          block the others. Sankey first — it's the highest-level
          view of how submissions move; the bar charts beneath are
          aggregate cuts. */}
      <ChartCard
        title="Submission flow"
        subtitle="How submissions move through the form. Click a node to filter the page to that step. Hover for counts."
      >
        <FlowFromQuery
          query={flowQ}
          activeKeys={filters.current_step}
          onNodeClick={toggleCurrentStep}
        />
      </ChartCard>

      <ChartCard
        title="Submissions by state"
        subtitle="Click a bar to filter the page to that state."
      >
        <ChartFromQuery
          query={stateQ}
          orientation="vertical"
          activeKeys={filters.state}
          onBarClick={toggleState}
        />
      </ChartCard>

      <ChartCard
        title="Submissions by current step"
        subtitle="Where in-flight submissions are parked. Terminal steps excluded."
      >
        <ChartFromQuery
          query={currentStepQ}
          orientation="horizontal"
          activeKeys={filters.current_step}
          onBarClick={toggleCurrentStep}
        />
      </ChartCard>

      <ChartCard
        title="Step reach counts"
        subtitle="How many submissions have ever reached each step. Useful for branch counts."
      >
        <ChartFromQuery
          query={stepCountsQ}
          orientation="horizontal"
          activeKeys={undefined}
          onBarClick={undefined}
        />
      </ChartCard>

      {/* Time charts. completion_time shows the overall distribution;
          step_time breaks it down per step with click-to-drilldown;
          throughput shows submissions started over time. All three
          respect the page-level filters (which default to terminal
          states only, so "completed work" is the default focus). */}
      <ChartCard
        title="Time to completion"
        subtitle="How long submissions take from start to terminal state."
      >
        <CompletionTimeFromQuery query={completionQ} />
      </ChartCard>

      <ChartCard
        title="Time per step"
        subtitle="Wall-clock time between a step starting and being submitted. A long mean usually means the user took a break, not that the step itself is slow."
      >
        <StepTimeFromQuery
          query={stepTimeQ}
          expandedStep={expandedStep}
          scaleMode={stepTimeScale}
          onScaleModeChange={setStepTimeScale}
          onBarClick={(nodeId) =>
            setExpandedStep(expandedStep === nodeId ? null : nodeId)
          }
        />
        {expandedStep ? (
          <div className="mt-4 border-t border-border pt-4">
            <p className="font-sans text-xs uppercase tracking-[0.16em] text-muted mb-2">
              Distribution: {expandedStep}
            </p>
            <StepHistogramFromQuery query={stepHistogramQ} />
          </div>
        ) : null}
      </ChartCard>

      <ChartCard
        title="Submissions over time"
        subtitle="Submissions started per time bucket, colored by current state."
      >
        <ThroughputFromQuery
          query={activeThroughputQ}
          selectedInterval={throughputInterval}
          onIntervalChange={setThroughputInterval}
        />
      </ChartCard>

      <ChartCard
        title="Submission rate"
        subtitle="Fine-grained submissions per time bucket. Spikes that daily bars smooth away — useful for spotting attack traffic, viral inbound, or other anomalies."
      >
        <SubmissionRateFromQuery
          query={activeSubmissionRateQ}
          selectedInterval={submissionRateInterval}
          onIntervalChange={setSubmissionRateInterval}
        />
      </ChartCard>
    </div>
  );
}

/** A single composite control for the date range, modeled on Looker's
 *  advanced date filter: a button shows the active selection, click
 *  opens a popover with named presets and a custom-range section
 *  (start + end inputs and an Apply button). Presets and custom
 *  range are mutually exclusive — picking a preset clears any
 *  custom dates, and committing custom dates clears the preset.
 *
 *  The button label is derived: `last_30_days` if neither preset
 *  nor custom is set (the framework default), `'Custom: A → B'`
 *  for an explicit pair, or the preset's display label otherwise.
 *  The popover closes on outside click and Escape. */
function DateRangeControl({
  preset,
  startDate,
  endDate,
  onPickPreset,
  onApplyCustom,
}: {
  preset: NonNullable<AnalyticsFilters["date_range"]> | null;
  startDate: string | null;
  endDate: string | null;
  onPickPreset: (
    r: NonNullable<AnalyticsFilters["date_range"]>,
  ) => void;
  onApplyCustom: (start: string, end: string) => void;
}) {
  const [open, setOpen] = useState(false);
  // Local draft state for the custom-range inputs. Seeded from the
  // current URL values so opening the popover after picking a
  // custom range pre-fills the inputs with what's active. Edits to
  // the drafts only take effect on Apply (per design — typing into
  // a date input fires nothing until the user commits).
  const [draftStart, setDraftStart] = useState(startDate ?? "");
  const [draftEnd, setDraftEnd] = useState(endDate ?? "");
  const wrapRef = useRef<HTMLDivElement | null>(null);

  // Re-seed drafts when the URL changes from outside (e.g. another
  // control cleared filters); otherwise stale draft values could
  // re-apply on the next custom commit.
  useEffect(() => {
    setDraftStart(startDate ?? "");
    setDraftEnd(endDate ?? "");
  }, [startDate, endDate]);

  // Close on outside click + Escape.
  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (!wrapRef.current) return;
      if (!wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Derive the button label. Custom range wins; otherwise show the
  // active preset (or the framework default).
  let label: string;
  if (startDate || endDate) {
    label = `Custom: ${startDate || "…"} → ${endDate || "…"}`;
  } else {
    const activePreset = preset ?? "last_30_days";
    label =
      DATE_RANGE_OPTIONS.find((o) => o.value === activePreset)?.label ??
      activePreset;
  }

  const customApplyDisabled =
    !draftStart && !draftEnd
      ? true
      : !!(draftStart && draftEnd && draftStart > draftEnd);

  return (
    <div ref={wrapRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 border border-border bg-bg px-3 py-1.5 font-mono text-xs text-ink hover:border-ink"
      >
        <span className="text-muted">Date range:</span>
        <span>{label}</span>
        <span className="text-muted">▾</span>
      </button>
      {open ? (
        <div className="absolute left-0 top-full z-10 mt-1 flex w-80 flex-col border border-border bg-surface shadow-lg">
          <ul className="flex flex-col">
            {DATE_RANGE_OPTIONS.map((o) => {
              const isActive =
                !startDate && !endDate &&
                (preset ?? "last_30_days") === o.value;
              return (
                <li key={o.value}>
                  <button
                    type="button"
                    onClick={() => {
                      onPickPreset(o.value);
                      setOpen(false);
                    }}
                    className={`flex w-full items-center px-3 py-2 text-left font-sans text-xs uppercase tracking-[0.12em] hover:bg-bg ${
                      isActive ? "text-ink" : "text-muted hover:text-ink"
                    }`}
                  >
                    {isActive ? "● " : "  "}
                    {o.label}
                  </button>
                </li>
              );
            })}
          </ul>
          <div className="flex flex-col gap-2 border-t border-border bg-bg px-3 py-3">
            <p className="font-sans text-xs uppercase tracking-[0.12em] text-muted">
              Custom range
            </p>
            <div className="flex items-center gap-1.5">
              <input
                type="date"
                value={draftStart}
                onChange={(e) => setDraftStart(e.target.value)}
                className="min-w-0 flex-1 border border-border bg-surface px-2 py-1 font-mono text-xs text-ink"
              />
              <span className="text-muted">→</span>
              <input
                type="date"
                value={draftEnd}
                onChange={(e) => setDraftEnd(e.target.value)}
                className="min-w-0 flex-1 border border-border bg-surface px-2 py-1 font-mono text-xs text-ink"
              />
            </div>
            <button
              type="button"
              disabled={customApplyDisabled}
              onClick={() => {
                onApplyCustom(draftStart, draftEnd);
                setOpen(false);
              }}
              className="self-end border border-border bg-ink px-3 py-1 font-sans text-xs uppercase tracking-[0.14em] text-bg hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-50"
            >
              Apply
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function ChartCard({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="border border-border bg-surface">
      <header className="border-b border-border px-4 py-3">
        <h3 className="font-display text-sm uppercase tracking-[0.18em] text-ink">
          {title}
        </h3>
        {subtitle ? (
          <p className="mt-1 text-xs text-muted">{subtitle}</p>
        ) : null}
      </header>
      <div className="px-4 py-4">{children}</div>
    </section>
  );
}

/** Generic chart renderer that handles loading/error/empty states
 *  uniformly. The bar style adapts to orientation; click-to-filter
 *  is opt-in via `onBarClick` and the active-key set styles selected
 *  bars distinctly while non-matching bars stay visible (greyed). */
function ChartFromQuery({
  query,
  orientation,
  activeKeys,
  onBarClick,
}: {
  query: { data?: AnalyticsResponse; error: unknown; isLoading: boolean };
  orientation: "vertical" | "horizontal";
  activeKeys: string[] | undefined;
  onBarClick: ((key: string) => void) | undefined;
}) {
  if (query.isLoading) {
    return <p className="text-xs text-muted">Loading…</p>;
  }
  if (query.error) {
    return (
      <p className="text-xs text-warning">
        {query.error instanceof Error
          ? query.error.message
          : "Failed to load."}
      </p>
    );
  }
  const data = query.data;
  if (!data || data.buckets.length === 0) {
    return (
      <p className="text-xs text-muted">No submissions in this range.</p>
    );
  }
  return (
    <BarChart
      buckets={data.buckets}
      orientation={orientation}
      activeKeys={activeKeys}
      onBarClick={onBarClick}
    />
  );
}

/** Analogous wrapper for the flow sankey — same loading/error/empty
 *  pattern as ChartFromQuery, but unwraps the FlowResponse and
 *  hands it to FlowSankey. Active-keys is shared with the current-
 *  step bar chart since both filter by node id. */
function FlowFromQuery({
  query,
  activeKeys,
  onNodeClick,
}: {
  query: { data?: FlowResponse; error: unknown; isLoading: boolean };
  activeKeys: string[] | undefined;
  onNodeClick: ((nodeId: string) => void) | undefined;
}) {
  if (query.isLoading) {
    return <p className="text-xs text-muted">Loading…</p>;
  }
  if (query.error) {
    return (
      <p className="text-xs text-warning">
        {query.error instanceof Error
          ? query.error.message
          : "Failed to load."}
      </p>
    );
  }
  const data = query.data;
  if (!data || data.total === 0) {
    return (
      <p className="text-xs text-muted">No submissions in this range.</p>
    );
  }
  return (
    <FlowSankey
      data={data}
      activeKeys={activeKeys}
      onNodeClick={onNodeClick}
    />
  );
}

/** Pre-format a "mean A · p50 B · p90 C" caption for histograms.
 *  Returns null when there's no data to summarize. */
function formatStatsCaption(
  total: number,
  mean: number | null,
  p50: number | null,
  p90: number | null,
): string | null {
  if (total === 0 || mean == null) return null;
  const parts = [`n = ${total}`];
  parts.push(`mean ${formatDuration(mean)}`);
  if (p50 != null) parts.push(`p50 ${formatDuration(p50)}`);
  if (p90 != null) parts.push(`p90 ${formatDuration(p90)}`);
  return parts.join(" · ");
}

function CompletionTimeFromQuery({
  query,
}: {
  query: {
    data?: CompletionTimeResponse;
    error: unknown;
    isLoading: boolean;
  };
}) {
  if (query.isLoading) return <p className="text-xs text-muted">Loading…</p>;
  if (query.error) {
    return (
      <p className="text-xs text-warning">
        {query.error instanceof Error
          ? query.error.message
          : "Failed to load."}
      </p>
    );
  }
  const data = query.data;
  if (!data || data.total === 0) {
    // When some submissions match but none have terminated_at, point
    // at the likely cause rather than just saying "no data."
    if (data && data.matching_submissions > 0) {
      return (
        <p className="text-xs text-muted">
          {data.matching_submissions} submission
          {data.matching_submissions === 1 ? "" : "s"} in this range, but
          none have a recorded completion time. This usually means the
          submission state is `success` or `failed` but `terminated_at`
          wasn't set — flag if this is unexpected.
        </p>
      );
    }
    return (
      <p className="text-xs text-muted">
        No completed submissions in this range.
      </p>
    );
  }
  return (
    <HistogramChart
      buckets={data.buckets}
      caption={formatStatsCaption(
        data.total, data.mean_seconds, data.p50_seconds, data.p90_seconds,
      )}
    />
  );
}

function StepTimeFromQuery({
  query,
  expandedStep,
  scaleMode,
  onScaleModeChange,
  onBarClick,
}: {
  query: { data?: StepTimeResponse; error: unknown; isLoading: boolean };
  expandedStep: string | null;
  scaleMode: "shared" | "per-step";
  onScaleModeChange: (m: "shared" | "per-step") => void;
  onBarClick: (nodeId: string) => void;
}) {
  if (query.isLoading) return <p className="text-xs text-muted">Loading…</p>;
  if (query.error) {
    return (
      <p className="text-xs text-warning">
        {query.error instanceof Error
          ? query.error.message
          : "Failed to load."}
      </p>
    );
  }
  const data = query.data;
  if (!data || data.steps.length === 0) {
    return <p className="text-xs text-muted">No timing data.</p>;
  }
  return (
    <StepTimeChart
      steps={data.steps}
      expandedNodeId={expandedStep}
      scaleMode={scaleMode}
      onScaleModeChange={onScaleModeChange}
      onBarClick={onBarClick}
    />
  );
}

function StepHistogramFromQuery({
  query,
}: {
  query: {
    data?: StepHistogramResponse;
    error: unknown;
    isLoading: boolean;
  };
}) {
  if (query.isLoading) return <p className="text-xs text-muted">Loading…</p>;
  if (query.error) {
    return (
      <p className="text-xs text-warning">
        {query.error instanceof Error
          ? query.error.message
          : "Failed to load."}
      </p>
    );
  }
  const data = query.data;
  if (!data || data.total === 0) {
    return <p className="text-xs text-muted">No visits to this step.</p>;
  }
  return (
    <HistogramChart
      buckets={data.buckets}
      caption={formatStatsCaption(
        data.total, data.mean_seconds, data.p50_seconds, data.p90_seconds,
      )}
    />
  );
}

function ThroughputFromQuery({
  query,
  selectedInterval,
  onIntervalChange,
}: {
  query: { data?: ThroughputResponse; error: unknown; isLoading: boolean };
  selectedInterval: ThroughputInterval | null;
  onIntervalChange: (i: ThroughputInterval | null) => void;
}) {
  if (query.isLoading) return <p className="text-xs text-muted">Loading…</p>;
  if (query.error) {
    return (
      <p className="text-xs text-warning">
        {query.error instanceof Error
          ? query.error.message
          : "Failed to load."}
      </p>
    );
  }
  const data = query.data;
  // Always render the chart (even when empty) so the interval
  // picker is visible — an empty chart in a "no data" state still
  // benefits from the picker being usable.
  return (
    <ThroughputChart
      buckets={data?.buckets ?? []}
      interval={data?.interval ?? "week"}
      selectedInterval={selectedInterval}
      onIntervalChange={onIntervalChange}
    />
  );
}

function SubmissionRateFromQuery({
  query,
  selectedInterval,
  onIntervalChange,
}: {
  query: { data?: SubmissionRateResponse; error: unknown; isLoading: boolean };
  selectedInterval: SubmissionRateInterval | null;
  onIntervalChange: (i: SubmissionRateInterval | null) => void;
}) {
  if (query.isLoading) return <p className="text-xs text-muted">Loading…</p>;
  if (query.error) {
    return (
      <p className="text-xs text-warning">
        {query.error instanceof Error
          ? query.error.message
          : "Failed to load."}
      </p>
    );
  }
  const data = query.data;
  if (!data || data.total === 0) {
    return (
      <p className="text-xs text-muted">
        No submissions in this range.
      </p>
    );
  }
  return (
    <SubmissionRateChart
      data={data}
      selectedInterval={selectedInterval}
      onIntervalChange={onIntervalChange}
    />
  );
}

/** Minimal bar chart. Width/height calculated from the bucket max
 *  so the longest bar fills the chart area; other bars scale down.
 *  Empty buckets (count 0) still render as a thin axis tick so the
 *  user can see "this category had nothing" rather than "this
 *  category doesn't exist." */
function BarChart({
  buckets,
  orientation,
  activeKeys,
  onBarClick,
}: {
  buckets: AnalyticsBucket[];
  orientation: "vertical" | "horizontal";
  activeKeys: string[] | undefined;
  onBarClick: ((key: string) => void) | undefined;
}) {
  const max = Math.max(1, ...buckets.map((b) => b.count));
  if (orientation === "vertical") {
    return (
      <div
        className="flex items-end gap-3 overflow-x-auto pb-2"
        style={{ height: 220 }}
      >
        {buckets.map((b) => {
          const isActive =
            activeKeys === undefined ||
            activeKeys.length === 0 ||
            activeKeys.includes(b.key);
          const heightPct = (b.count / max) * 100;
          return (
            <button
              key={b.key}
              type="button"
              onClick={onBarClick ? () => onBarClick(b.key) : undefined}
              disabled={!onBarClick}
              className={`flex flex-col items-center gap-1 ${
                onBarClick ? "cursor-pointer" : "cursor-default"
              }`}
              style={{ height: "100%", minWidth: 64 }}
              title={`${b.label}: ${b.count}`}
            >
              <span className="font-mono text-xs text-muted">
                {b.count}
              </span>
              <div
                className={`w-12 transition-opacity ${
                  isActive
                    ? "bg-ink"
                    : "bg-ink opacity-25"
                }`}
                style={{
                  height: `${Math.max(2, heightPct)}%`,
                }}
              />
              <span className="font-sans text-xs uppercase tracking-[0.12em] text-ink text-center">
                {b.label}
              </span>
            </button>
          );
        })}
      </div>
    );
  }
  // Horizontal — labels on the left, bars extending right.
  return (
    <div className="flex flex-col gap-2">
      {buckets.map((b) => {
        const isActive =
          activeKeys === undefined ||
          activeKeys.length === 0 ||
          activeKeys.includes(b.key);
        const widthPct = (b.count / max) * 100;
        return (
          <button
            key={b.key}
            type="button"
            onClick={onBarClick ? () => onBarClick(b.key) : undefined}
            disabled={!onBarClick}
            className={`flex w-full items-center gap-3 text-left ${
              onBarClick ? "cursor-pointer" : "cursor-default"
            }`}
            title={`${b.label}: ${b.count}`}
          >
            <span
              className="font-sans text-xs uppercase tracking-[0.12em] text-ink"
              style={{ minWidth: 160 }}
            >
              {b.label}
            </span>
            <div className="flex-1 bg-bg" style={{ height: 18 }}>
              <div
                className={`h-full transition-opacity ${
                  isActive ? "bg-ink" : "bg-ink opacity-25"
                }`}
                style={{ width: `${Math.max(0.5, widthPct)}%` }}
              />
            </div>
            <span
              className="font-mono text-xs text-muted"
              style={{ minWidth: 32, textAlign: "right" }}
            >
              {b.count}
            </span>
          </button>
        );
      })}
    </div>
  );
}
