import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useSearchParams } from "react-router-dom";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  ApiError,
  deleteSubmissions,
  getFormSubmissionCurrentSteps,
  type CurrentStepOption,
  type SortEntry,
  type SortDirection,
  type SubmissionSummary,
} from "../../lib/api";
import { formatTimestamp } from "../../lib/format";
import { useFormSubmissions } from "../../hooks/useFormSubmissions";
import { useAuth } from "../../auth/AuthContext";
import { Modal } from "../ui/Modal";
import { StatePill } from "../listing/StatePill";

const PAGE_SIZE = 25;

interface SubmissionsTabProps {
  formId: string;
  onOpenSubmission: (id: string) => void;
}

// The state values a submission can have. Multi-select filter
// surfaces these as toggle chips at the top of the listing.
const STATE_VALUES = ["running", "success", "failed"] as const;
type StateValue = (typeof STATE_VALUES)[number];

// Sortable columns. The string keys match the backend's whitelist
// (_LISTING_SORT_COLUMNS in store.py); the header labels are what
// the user sees in the table. `current_step` is intentionally
// absent — backend doesn't sort on derived fields yet.
const SORT_COLUMNS = {
  submission_id: "Submission",
  state: "State",
  form_version: "Version",
  created_at: "Started",
  updated_at: "Last activity",
} as const;
type SortColumn = keyof typeof SORT_COLUMNS;
const SORTABLE_COLUMN_KEYS = Object.keys(SORT_COLUMNS) as SortColumn[];

// URL-backed listing state ---------------------------------------------------
//
// Filters, sort, and the current page live in the URL so a
// filtered view is shareable / bookmarkable. The hook in this
// section reads them on render and exposes setters that update
// `searchParams` — which then drives the next data fetch.
//
// URL shape:
//   ?state=running,failed     (comma-joined multi-select)
//   ?q=alpha                  (substring search)
//   ?sort=updated_at:desc,state:asc   (comma-joined sort spec)
//   ?page=2                   (1-indexed page number)
//   ?updated_since=2025-06-01 (calendar date, inclusive)
//   ?updated_before=2025-06-30 (calendar date, inclusive — backend
//                              bumps it to start-of-next-day under
//                              the hood so the day is captured)
//   ?step=upload,review       (comma-joined current_step filter)
//   ?show_deleted=1           (admin-only; include tombstoned rows)

interface UrlListingState {
  states: StateValue[];
  q: string;
  sort: SortEntry[];
  page: number; // 1-indexed
  updatedSince: string; // YYYY-MM-DD or empty
  updatedBefore: string;
  steps: string[];
  showDeleted: boolean;
}

// Permissive — the date input emits YYYY-MM-DD, but a hand-edited
// URL could carry something else. We don't validate here (backend
// drops malformed values), just preserve what's there.
function _validDate(raw: string): string {
  return /^\d{4}-\d{2}-\d{2}$/.test(raw) ? raw : "";
}

function parseUrl(params: URLSearchParams): UrlListingState {
  const stateRaw = params.get("state") ?? "";
  const states = stateRaw
    .split(",")
    .map((s) => s.trim())
    .filter((s): s is StateValue =>
      (STATE_VALUES as readonly string[]).includes(s),
    );

  const sortRaw = params.get("sort") ?? "";
  const sort: SortEntry[] = [];
  for (const entry of sortRaw.split(",")) {
    const [col, dir] = entry.split(":");
    if (
      col &&
      (SORTABLE_COLUMN_KEYS as readonly string[]).includes(col) &&
      (dir === "asc" || dir === "desc")
    ) {
      sort.push({ column: col, direction: dir as SortDirection });
    }
  }

  const pageRaw = parseInt(params.get("page") ?? "1", 10);
  const page = Number.isFinite(pageRaw) && pageRaw >= 1 ? pageRaw : 1;

  const stepRaw = params.get("step") ?? "";
  const steps = stepRaw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

  return {
    states,
    q: params.get("q") ?? "",
    sort,
    page,
    updatedSince: _validDate(params.get("updated_since") ?? ""),
    updatedBefore: _validDate(params.get("updated_before") ?? ""),
    steps,
    showDeleted: params.get("show_deleted") === "1",
  };
}

/**
 * The form's submission listing — server-paginated, server-filtered,
 * server-sorted. The URL holds the user's filter/sort/page state so
 * a chosen view is bookmarkable and survives refresh.
 *
 * Admins additionally get bulk soft-delete (per-row checkbox →
 * confirm modal → POST). Selecting a row only persists within the
 * current page set; changing filters, sort, or page clears the
 * selection so the user can't accidentally act on rows they no
 * longer see.
 */
export function SubmissionsTab({
  formId,
  onOpenSubmission,
}: SubmissionsTabProps) {
  const [searchParams, setSearchParams] = useSearchParams();
  const urlState = useMemo(() => parseUrl(searchParams), [searchParams]);
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const canDelete = !!user?.is_admin;

  // Build the server query from URL state. `offset` is derived
  // from page; the rest pass through.
  const query = useMemo(
    () => ({
      limit: PAGE_SIZE,
      offset: (urlState.page - 1) * PAGE_SIZE,
      states: urlState.states.length > 0 ? urlState.states : undefined,
      q: urlState.q || undefined,
      sort: urlState.sort.length > 0 ? urlState.sort : undefined,
      updatedSince: urlState.updatedSince || undefined,
      updatedBefore: urlState.updatedBefore || undefined,
      currentSteps:
        urlState.steps.length > 0 ? urlState.steps : undefined,
      showDeleted: urlState.showDeleted || undefined,
    }),
    [urlState],
  );

  const { data, error, isLoading, isFetching } = useFormSubmissions(
    formId, query,
  );

  // Per-row selection. Cleared when the visible page set changes
  // (any filter, sort, page, or show-deleted toggle) — keeping a
  // selection across page boundaries would let a delete act on
  // rows the user can't see, which is a worse failure mode than
  // requiring them to re-select after a paginate.
  const [selected, setSelected] = useState<Set<string>>(new Set());
  useEffect(() => {
    setSelected(new Set());
  }, [
    urlState.page,
    urlState.q,
    urlState.states.join(","),
    urlState.sort.map((s) => `${s.column}:${s.direction}`).join(","),
    urlState.updatedSince,
    urlState.updatedBefore,
    urlState.steps.join(","),
    urlState.showDeleted,
  ]);

  const [confirmOpen, setConfirmOpen] = useState(false);
  const [lastResult, setLastResult] = useState<
    { deleted: number; not_found: number } | null
  >(null);

  // --- URL mutators -------------------------------------------------------
  //
  // Each UrlListingState field has an explicit URL param name and
  // encoder. Field-name and URL-param-name differ for the snake_case
  // backend params (`updatedSince` ↔ `updated_since`, etc.), so a
  // single-source mapping keeps the two views in sync.

  const setUrl = useCallback(
    (partial: Partial<UrlListingState>) => {
      const next = new URLSearchParams(searchParams);
      const apply = (
        urlKey: string,
        encoded: string,
      ) => {
        if (encoded) next.set(urlKey, encoded);
        else next.delete(urlKey);
      };
      if (partial.states !== undefined) {
        apply("state", partial.states.join(","));
      }
      if (partial.q !== undefined) {
        apply("q", partial.q.trim());
      }
      if (partial.sort !== undefined) {
        apply(
          "sort",
          partial.sort
            .map((s) => `${s.column}:${s.direction}`)
            .join(","),
        );
      }
      if (partial.page !== undefined) {
        // omit page=1 from URL — keeps default URLs clean
        apply("page", partial.page === 1 ? "" : String(partial.page));
      }
      if (partial.updatedSince !== undefined) {
        apply("updated_since", partial.updatedSince);
      }
      if (partial.updatedBefore !== undefined) {
        apply("updated_before", partial.updatedBefore);
      }
      if (partial.steps !== undefined) {
        apply("step", partial.steps.join(","));
      }
      if (partial.showDeleted !== undefined) {
        // Presence-only flag — omit when false.
        apply("show_deleted", partial.showDeleted ? "1" : "");
      }
      setSearchParams(next, { replace: false });
    },
    [searchParams, setSearchParams],
  );

  // Changing filter or sort resets to page 1 — otherwise the user
  // ends up on an empty page 5 when the new filter has <100 rows.
  const setStates = useCallback(
    (next: StateValue[]) => setUrl({ states: next, page: 1 }),
    [setUrl],
  );
  const setQ = useCallback(
    (next: string) => setUrl({ q: next, page: 1 }),
    [setUrl],
  );
  const setSort = useCallback(
    (next: SortEntry[]) => setUrl({ sort: next, page: 1 }),
    [setUrl],
  );
  const setPage = useCallback(
    (next: number) => setUrl({ page: next }),
    [setUrl],
  );
  const setUpdatedSince = useCallback(
    (next: string) => setUrl({ updatedSince: next, page: 1 }),
    [setUrl],
  );
  const setUpdatedBefore = useCallback(
    (next: string) => setUrl({ updatedBefore: next, page: 1 }),
    [setUrl],
  );
  const setSteps = useCallback(
    (next: string[]) => setUrl({ steps: next, page: 1 }),
    [setUrl],
  );
  const setShowDeleted = useCallback(
    (next: boolean) => setUrl({ showDeleted: next, page: 1 }),
    [setUrl],
  );
  const clearAllFilters = useCallback(() => {
    setUrl({
      states: [], q: "", updatedSince: "", updatedBefore: "",
      steps: [], showDeleted: false, page: 1,
    });
  }, [setUrl]);

  // --- Selection helpers --------------------------------------------------

  const pageRows = data?.submissions ?? [];
  const allOnPageHandles = useMemo(
    () => pageRows.map((r) => r.handle),
    [pageRows],
  );
  const allOnPageSelected =
    allOnPageHandles.length > 0 &&
    allOnPageHandles.every((h) => selected.has(h));

  const toggleAllOnPage = useCallback(() => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (allOnPageSelected) {
        for (const h of allOnPageHandles) next.delete(h);
      } else {
        for (const h of allOnPageHandles) next.add(h);
      }
      return next;
    });
  }, [allOnPageSelected, allOnPageHandles]);

  const toggleOne = useCallback((handle: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(handle)) next.delete(handle);
      else next.add(handle);
      return next;
    });
  }, []);

  const clearSelection = useCallback(() => {
    setSelected(new Set());
    setLastResult(null);
  }, []);

  // --- Delete mutation ----------------------------------------------------

  const deleteMutation = useMutation({
    mutationFn: async (handles: string[]) =>
      deleteSubmissions(formId, handles),
    onSuccess: (resp) => {
      setLastResult({
        deleted: resp.deleted.length,
        not_found: resp.not_found.length,
      });
      setSelected(new Set());
      setConfirmOpen(false);
      // Refetch the listing + the form's summary KPI counts.
      queryClient.invalidateQueries({
        queryKey: ["formSubmissions", formId],
      });
      queryClient.invalidateQueries({
        queryKey: ["formSummary", formId],
      });
    },
  });

  // --- Sort header click handler -----------------------------------------
  //
  // Plain click semantics: replace the spec with just this column.
  // If it's already the only sort, cycle asc → desc → reset (back
  // to the default created_at:desc).
  //
  // Shift+click semantics: toggle this column in the existing
  // spec. Absent → append asc. Asc → desc. Desc → drop entirely.
  // The shift modifier matches Excel / Linear / Notion conventions
  // for additive multi-column sort.
  const onSortHeaderClick = useCallback(
    (column: SortColumn, shiftKey: boolean) => {
      const existing = urlState.sort.find((s) => s.column === column);
      if (shiftKey) {
        if (!existing) {
          setSort([
            ...urlState.sort, { column, direction: "asc" },
          ]);
        } else if (existing.direction === "asc") {
          setSort(urlState.sort.map((s) =>
            s.column === column
              ? { ...s, direction: "desc" as const }
              : s,
          ));
        } else {
          // already desc → remove from spec
          setSort(urlState.sort.filter((s) => s.column !== column));
        }
        return;
      }
      // Plain click — single-column path.
      if (!existing) {
        setSort([{ column, direction: "asc" }]);
      } else if (urlState.sort.length === 1) {
        // Single column already; cycle direction or reset.
        if (existing.direction === "asc") {
          setSort([{ column, direction: "desc" }]);
        } else {
          setSort([]); // back to server default (created_at desc)
        }
      } else {
        // Multi-column → collapse to just this one at asc.
        setSort([{ column, direction: "asc" }]);
      }
    },
    [urlState.sort, setSort],
  );

  // --- Render -------------------------------------------------------------

  if (isLoading && !data) {
    return (
      <p className="font-display text-2xl text-ink opacity-30">Loading…</p>
    );
  }
  if (error) {
    return (
      <div className="border border-error bg-surface p-6">
        <p className="text-error text-sm">
          {error instanceof ApiError && error.status === 404
            ? "This form doesn't exist."
            : `Couldn't load submissions: ${
                error instanceof ApiError ? error.message : "unknown error"
              }`}
        </p>
      </div>
    );
  }

  const total = data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const safePage = Math.min(urlState.page, pageCount);
  const rangeStart = total === 0 ? 0 : (safePage - 1) * PAGE_SIZE + 1;
  const rangeEnd = Math.min(rangeStart + PAGE_SIZE - 1, total);
  const showEmptyMessage = total === 0;

  return (
    <div>
      <FilterBar
        formId={formId}
        states={urlState.states}
        q={urlState.q}
        updatedSince={urlState.updatedSince}
        updatedBefore={urlState.updatedBefore}
        steps={urlState.steps}
        showDeleted={urlState.showDeleted}
        canShowDeleted={canDelete}
        onStatesChange={setStates}
        onQChange={setQ}
        onUpdatedSinceChange={setUpdatedSince}
        onUpdatedBeforeChange={setUpdatedBefore}
        onStepsChange={setSteps}
        onShowDeletedChange={setShowDeleted}
        onClearAll={clearAllFilters}
      />
      {/* Action-bar slot — reserved at a fixed height so the table
          doesn't shift when its contents appear or disappear. Both
          the bulk-action bar (admin, when rows are selected) and
          the delete-result toast (briefly, after a delete) share
          this slot. For non-admin users the slot isn't rendered at
          all — they never see either piece of UI, so reserving
          space for them would be permanent dead space. For admins
          the slot is always present; an empty slot is a stable,
          predictable affordance ("this is where bulk actions
          surface") and avoids the layout shift Connor flagged
          when a checkbox first goes on.

          Height matches the natural bar height: py-2 (16px) + text
          line-height + 1px borders ≈ 36–38px. h-10 (40px) gives a
          1–2px breathing band so the bar's border doesn't clip
          against the slot edge. */}
      {canDelete ? (
        <div className="mb-3 h-10">
          {selected.size > 0 ? (
            <BulkActionBar
              selectedCount={selected.size}
              onDelete={() => setConfirmOpen(true)}
              onClear={clearSelection}
              isPending={deleteMutation.isPending}
            />
          ) : lastResult ? (
            <DeleteResultToast
              result={lastResult}
              onDismiss={() => setLastResult(null)}
            />
          ) : null}
        </div>
      ) : null}
      <div className="relative overflow-x-auto">
        {isFetching && data ? (
          // Subtle "refreshing" indicator — keeps the prior page
          // visible (keepPreviousData) while the new one fetches,
          // so the table doesn't flash empty mid-paginate.
          <div
            aria-hidden
            className="pointer-events-none absolute right-0 top-0 z-10 px-2 py-1 font-mono text-[10px] text-muted"
          >
            refreshing…
          </div>
        ) : null}
        <table className="w-full border-collapse">
          <thead>
            <tr className="border-b border-border">
              {canDelete ? (
                <th
                  className="w-8 py-2 pr-2 text-left"
                  onClick={(e) => e.stopPropagation()}
                >
                  <input
                    type="checkbox"
                    aria-label={
                      allOnPageSelected
                        ? "Deselect all rows on this page"
                        : "Select all rows on this page"
                    }
                    checked={allOnPageSelected}
                    onChange={toggleAllOnPage}
                  />
                </th>
              ) : null}
              {SORTABLE_COLUMN_KEYS.map((col) => {
                const sortIdx = urlState.sort.findIndex(
                  (s) => s.column === col,
                );
                const sortEntry =
                  sortIdx >= 0 ? urlState.sort[sortIdx] : null;
                return (
                  <SortHeader
                    key={col}
                    label={SORT_COLUMNS[col]}
                    direction={sortEntry?.direction}
                    priority={
                      sortEntry && urlState.sort.length > 1
                        ? sortIdx + 1
                        : null
                    }
                    onClick={(e) =>
                      onSortHeaderClick(col, e.shiftKey)
                    }
                  />
                );
              })}
              <Th>Step</Th>
            </tr>
          </thead>
          <tbody>
            {pageRows.map((s) => (
              <SubmissionRow
                key={s.handle}
                s={s}
                onOpen={onOpenSubmission}
                selectable={canDelete}
                selected={selected.has(s.handle)}
                onToggleSelected={() => toggleOne(s.handle)}
              />
            ))}
          </tbody>
        </table>
        {showEmptyMessage ? (
          <p className="py-6 text-muted text-sm">
            {urlState.q || urlState.states.length > 0
              ? "No submissions match your filters."
              : "No submissions yet for this form."}
          </p>
        ) : null}
      </div>
      {pageCount > 1 ? (
        <Paginator
          page={safePage}
          pageCount={pageCount}
          rangeStart={rangeStart}
          rangeEnd={rangeEnd}
          total={total}
          onChange={setPage}
        />
      ) : null}
      <ConfirmDeleteModal
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        handles={Array.from(selected)}
        submissions={pageRows}
        onConfirm={() => deleteMutation.mutate(Array.from(selected))}
        isPending={deleteMutation.isPending}
        errorMessage={
          deleteMutation.isError
            ? deleteMutation.error instanceof ApiError
              ? deleteMutation.error.message
              : "Couldn't delete — try again."
            : null
        }
      />
    </div>
  );
}

// --- FilterBar --------------------------------------------------------------
//
// The listing's filter row(s). Each filter is URL-backed via the
// parent's setters; this component is dumb. Row 1: search input,
// state chips, show-deleted toggle (admin). Row 2: date range
// (updated_at), current-step dropdown, clear-all link.
//
// The search input debounces 250ms so each keystroke doesn't fire
// a backend request — typing "alpha" otherwise triggers five
// separate page fetches.

function FilterBar({
  formId,
  states,
  q,
  updatedSince,
  updatedBefore,
  steps,
  showDeleted,
  canShowDeleted,
  onStatesChange,
  onQChange,
  onUpdatedSinceChange,
  onUpdatedBeforeChange,
  onStepsChange,
  onShowDeletedChange,
  onClearAll,
}: {
  formId: string;
  states: StateValue[];
  q: string;
  updatedSince: string;
  updatedBefore: string;
  steps: string[];
  showDeleted: boolean;
  canShowDeleted: boolean;
  onStatesChange: (next: StateValue[]) => void;
  onQChange: (next: string) => void;
  onUpdatedSinceChange: (next: string) => void;
  onUpdatedBeforeChange: (next: string) => void;
  onStepsChange: (next: string[]) => void;
  onShowDeletedChange: (next: boolean) => void;
  onClearAll: () => void;
}) {
  // Local mirror so typing is responsive; sync to parent via debounce.
  const [localQ, setLocalQ] = useState(q);
  useEffect(() => {
    setLocalQ(q);
  }, [q]);
  useEffect(() => {
    if (localQ === q) return;
    const t = window.setTimeout(() => onQChange(localQ), 250);
    return () => window.clearTimeout(t);
  }, [localQ, q, onQChange]);

  const toggleState = (s: StateValue) => {
    if (states.includes(s)) onStatesChange(states.filter((x) => x !== s));
    else onStatesChange([...states, s]);
  };

  const anyFilterSet =
    states.length > 0 ||
    q !== "" ||
    updatedSince !== "" ||
    updatedBefore !== "" ||
    steps.length > 0 ||
    showDeleted;

  return (
    <div className="mb-3 flex flex-col gap-2">
      {/* Row 1: search | state chips | show-deleted */}
      <div className="flex flex-wrap items-center gap-3">
        <input
          type="search"
          value={localQ}
          onChange={(e) => setLocalQ(e.target.value)}
          placeholder="Search by submission id or handle…"
          className="flex-1 min-w-[16rem] border border-border bg-bg px-3 py-1.5 font-mono text-xs text-ink placeholder:text-muted"
          aria-label="Search submissions"
        />
        <div className="flex items-center gap-1">
          {STATE_VALUES.map((s) => {
            const active = states.includes(s);
            return (
              <button
                key={s}
                type="button"
                onClick={() => toggleState(s)}
                className={
                  active
                    ? "border border-ink bg-ink px-3 py-1 font-mono text-xs text-bg"
                    : "border border-border bg-bg px-3 py-1 font-mono text-xs text-muted hover:text-ink"
                }
                aria-pressed={active}
              >
                {s}
              </button>
            );
          })}
        </div>
        {canShowDeleted ? (
          <label className="flex items-center gap-1.5 font-mono text-xs text-muted hover:text-ink cursor-pointer">
            <input
              type="checkbox"
              checked={showDeleted}
              onChange={(e) => onShowDeletedChange(e.target.checked)}
            />
            <span>Show deleted</span>
          </label>
        ) : null}
      </div>

      {/* Row 2: date range | current-step dropdown | clear-all */}
      <div className="flex flex-wrap items-center gap-3">
        <DateRangeInput
          since={updatedSince}
          before={updatedBefore}
          onSinceChange={onUpdatedSinceChange}
          onBeforeChange={onUpdatedBeforeChange}
        />
        <CurrentStepFilter
          formId={formId}
          selectedSteps={steps}
          showDeleted={showDeleted}
          onChange={onStepsChange}
        />
        {anyFilterSet ? (
          <button
            type="button"
            onClick={onClearAll}
            className="ml-auto font-mono text-xs text-muted hover:text-ink"
            aria-label="Clear all filters"
          >
            Clear all filters
          </button>
        ) : null}
      </div>
    </div>
  );
}

// --- DateRangeInput ---------------------------------------------------------

function DateRangeInput({
  since,
  before,
  onSinceChange,
  onBeforeChange,
}: {
  since: string;
  before: string;
  onSinceChange: (next: string) => void;
  onBeforeChange: (next: string) => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-muted">
        Last activity
      </span>
      <input
        type="date"
        value={since}
        onChange={(e) => onSinceChange(e.target.value)}
        // The native input emits "" when the user clears the field
        // via the "x" button; we feed that straight to the parent
        // which drops the URL param.
        className="border border-border bg-bg px-2 py-1 font-mono text-xs text-ink"
        aria-label="Last activity — from"
      />
      <span className="font-mono text-xs text-muted">→</span>
      <input
        type="date"
        value={before}
        onChange={(e) => onBeforeChange(e.target.value)}
        className="border border-border bg-bg px-2 py-1 font-mono text-xs text-ink"
        aria-label="Last activity — to"
      />
    </div>
  );
}

// --- CurrentStepFilter ------------------------------------------------------
//
// Dropdown of available current-step values, populated lazily on
// open. Re-fetches when `showDeleted` changes (the options endpoint
// honors the same flag so counts stay truthful).

function CurrentStepFilter({
  formId,
  selectedSteps,
  showDeleted,
  onChange,
}: {
  formId: string;
  selectedSteps: string[];
  showDeleted: boolean;
  onChange: (next: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const { data: options, isLoading } = useQuery<CurrentStepOption[]>({
    queryKey: ["formCurrentSteps", formId, showDeleted],
    queryFn: () =>
      getFormSubmissionCurrentSteps(formId, { showDeleted }),
    enabled: open, // lazy — don't hit the endpoint until the user opens
    staleTime: 30_000,
  });

  const summary =
    selectedSteps.length === 0
      ? "Step: All"
      : selectedSteps.length === 1
        ? `Step: ${selectedSteps[0]}`
        : `Step: ${selectedSteps.length} selected`;

  const toggle = (node_id: string) => {
    if (selectedSteps.includes(node_id)) {
      onChange(selectedSteps.filter((s) => s !== node_id));
    } else {
      onChange([...selectedSteps, node_id]);
    }
  };

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={
          selectedSteps.length > 0
            ? "border border-ink bg-ink px-3 py-1 font-mono text-xs text-bg"
            : "border border-border bg-bg px-3 py-1 font-mono text-xs text-muted hover:text-ink"
        }
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        {summary}
      </button>
      {open ? (
        <>
          {/* Backdrop captures click-outside to close. Behind the
              menu (z-40), so clicks on the menu itself fall
              through to the menu's own handlers. */}
          <div
            className="fixed inset-0 z-40"
            onClick={() => setOpen(false)}
          />
          {/* Menu anchored under the trigger via the parent's
              `relative` positioning context. `top-full` puts it
              flush against the bottom edge of the button; `left-0`
              aligns to the left edge so the menu opens rightward
              into available space. Without these the menu falls
              back to its natural flow position, which inside a
              full-viewport fixed parent puts it at the page's
              top-left corner. */}
          <div
            onClick={(e) => e.stopPropagation()}
            className="absolute left-0 top-full z-50 mt-1 min-w-[14rem] border border-border bg-bg shadow-lg"
          >
            <CurrentStepFilterMenu
              isLoading={isLoading}
              options={options ?? []}
              selected={selectedSteps}
              onToggle={toggle}
              onClear={() => onChange([])}
            />
          </div>
        </>
      ) : null}
    </div>
  );
}

function CurrentStepFilterMenu({
  isLoading,
  options,
  selected,
  onToggle,
  onClear,
}: {
  isLoading: boolean;
  options: CurrentStepOption[];
  selected: string[];
  onToggle: (node_id: string) => void;
  onClear: () => void;
}) {
  if (isLoading) {
    return (
      <div className="p-3 font-mono text-xs text-muted">Loading…</div>
    );
  }
  if (options.length === 0) {
    return (
      <div className="p-3 font-mono text-xs text-muted">
        No steps to filter on yet.
      </div>
    );
  }
  return (
    <div className="max-h-72 overflow-y-auto">
      <ul className="py-1">
        {options.map((o) => (
          <li key={o.node_id}>
            <label className="flex items-center gap-2 px-3 py-1.5 font-mono text-xs hover:bg-surface cursor-pointer">
              <input
                type="checkbox"
                checked={selected.includes(o.node_id)}
                onChange={() => onToggle(o.node_id)}
              />
              <span className="flex-1 truncate text-ink">{o.node_id}</span>
              <span className="text-muted tabular-nums">{o.count}</span>
            </label>
          </li>
        ))}
      </ul>
      {selected.length > 0 ? (
        <div className="border-t border-border px-3 py-1.5">
          <button
            type="button"
            onClick={onClear}
            className="font-mono text-xs text-muted hover:text-ink"
          >
            Clear step filter
          </button>
        </div>
      ) : null}
    </div>
  );
}

// --- SortHeader -------------------------------------------------------------

function SortHeader({
  label,
  direction,
  priority,
  onClick,
}: {
  label: string;
  direction?: SortDirection;
  /** 1-indexed position in the multi-column sort — only set when
   *  the spec has 2+ entries (single-sort doesn't need a badge). */
  priority: number | null;
  onClick: (e: React.MouseEvent<HTMLButtonElement>) => void;
}) {
  const arrow = direction === "asc" ? "↑" : direction === "desc" ? "↓" : "";
  return (
    <th
      className="py-2 pr-6 text-left font-mono text-[11px] font-medium uppercase tracking-wider text-muted"
      onClick={(e) => e.stopPropagation()}
    >
      <button
        type="button"
        onClick={onClick}
        title="Click to sort. Shift+click to add to multi-column sort."
        className={
          direction
            ? "flex items-center gap-1 text-ink hover:text-ink"
            : "flex items-center gap-1 hover:text-ink"
        }
      >
        <span>{label}</span>
        {arrow ? <span aria-hidden>{arrow}</span> : null}
        {priority !== null ? (
          <span
            className="ml-0.5 inline-block min-w-[1ch] border border-border bg-bg px-1 text-[9px] leading-tight text-muted"
            aria-label={`Sort priority ${priority}`}
          >
            {priority}
          </span>
        ) : null}
      </button>
    </th>
  );
}

function Th({ children }: { children: ReactNode }) {
  return (
    <th className="py-2 pr-6 text-left font-mono text-[11px] font-medium uppercase tracking-wider text-muted">
      {children}
    </th>
  );
}

// --- BulkActionBar / DeleteResultToast --------------------------------------

function BulkActionBar({
  selectedCount,
  onDelete,
  onClear,
  isPending,
}: {
  selectedCount: number;
  onDelete: () => void;
  onClear: () => void;
  isPending: boolean;
}) {
  return (
    <div className="flex items-center justify-between border border-border bg-surface px-3 py-2">
      <span className="font-mono text-xs text-ink">
        {selectedCount} selected
      </span>
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={onClear}
          className="font-mono text-xs text-muted hover:text-ink"
        >
          Clear selection
        </button>
        <button
          type="button"
          onClick={onDelete}
          disabled={isPending}
          className="border border-error bg-bg px-3 py-1 font-mono text-xs text-error hover:bg-error hover:text-bg disabled:opacity-50"
        >
          Delete
        </button>
      </div>
    </div>
  );
}

function DeleteResultToast({
  result,
  onDismiss,
}: {
  result: { deleted: number; not_found: number };
  onDismiss: () => void;
}) {
  const parts: string[] = [];
  if (result.deleted > 0) parts.push(`${result.deleted} deleted`);
  if (result.not_found > 0) parts.push(`${result.not_found} not found`);
  return (
    <div className="flex items-center justify-between border border-border bg-surface px-3 py-2">
      <span className="font-mono text-xs text-ink">{parts.join(", ")}</span>
      <button
        type="button"
        onClick={onDismiss}
        className="font-mono text-xs text-muted hover:text-ink"
        aria-label="Dismiss"
      >
        ×
      </button>
    </div>
  );
}

// --- ConfirmDeleteModal -----------------------------------------------------

function ConfirmDeleteModal({
  open, onClose, handles, submissions, onConfirm, isPending, errorMessage,
}: {
  open: boolean;
  onClose: () => void;
  handles: string[];
  submissions: SubmissionSummary[];
  onConfirm: () => void;
  isPending: boolean;
  errorMessage: string | null;
}) {
  const lookup = useMemo(
    () => new Map(submissions.map((s) => [s.handle, s])),
    [submissions],
  );
  const previewIds: string[] = [];
  for (const h of handles.slice(0, 5)) {
    const row = lookup.get(h);
    previewIds.push(row?.submission_id ?? h);
  }
  const overflow = Math.max(0, handles.length - previewIds.length);
  return (
    <Modal open={open} onClose={onClose}>
      <div className="flex flex-col gap-4 p-5">
        <h2 className="font-display text-xl text-ink">
          Delete {handles.length} submission
          {handles.length === 1 ? "" : "s"}?
        </h2>
        <p className="text-sm text-muted">
          The submission rows are tombstoned — the data stays in the
          database for audit and can be restored from the backend, but
          they will no longer appear in this listing or in the form's
          stats.
        </p>
        <ul className="font-mono text-xs text-ink">
          {previewIds.map((id) => (
            <li key={id} className="truncate">
              {id}
            </li>
          ))}
          {overflow > 0 ? (
            <li className="text-muted">and {overflow} more</li>
          ) : null}
        </ul>
        {errorMessage ? (
          <p className="text-error text-xs">{errorMessage}</p>
        ) : null}
        <div className="flex items-center justify-end gap-3">
          <button
            type="button"
            onClick={onClose}
            disabled={isPending}
            className="font-mono text-xs text-muted hover:text-ink disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={isPending}
            className="border border-error bg-bg px-3 py-1 font-mono text-xs text-error hover:bg-error hover:text-bg disabled:opacity-50"
          >
            {isPending ? "Deleting…" : "Delete"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// --- SubmissionRow ----------------------------------------------------------

function SubmissionRow({
  s,
  onOpen,
  selectable,
  selected,
  onToggleSelected,
}: {
  s: SubmissionSummary;
  onOpen: (id: string) => void;
  selectable: boolean;
  selected: boolean;
  onToggleSelected: () => void;
}) {
  const id = s.submission_id ?? s.handle;
  // Tombstoned rows are non-interactive — the in-memory submission
  // was evicted on soft-delete, so the detail page would 404. The
  // row stays visible (for admins via show_deleted) for audit; a
  // separate restore endpoint is the planned undelete path.
  const isDeleted = !!s.deleted_at;
  return (
    <tr
      onClick={() => {
        if (!isDeleted) onOpen(id);
      }}
      className={
        isDeleted
          ? "border-b border-border opacity-60"
          : "cursor-pointer border-b border-border transition-colors hover:bg-surface"
      }
    >
      {selectable ? (
        <td
          className="w-8 py-4 pr-2"
          onClick={(e) => e.stopPropagation()}
        >
          <input
            type="checkbox"
            aria-label={`Select submission ${id}`}
            checked={selected}
            // Allow selecting a tombstoned row only insofar as the
            // bulk-delete endpoint already treats them as
            // not_found (the store fn excludes already-deleted
            // handles). Keeping the checkbox enabled keeps the
            // header select-all consistent; the action just no-ops
            // on the deleted handle.
            onChange={onToggleSelected}
          />
        </td>
      ) : null}
      <td className="py-4 pr-6">
        <span
          className={
            isDeleted
              ? "font-mono text-sm text-muted line-through"
              : "font-mono text-sm text-ink"
          }
        >
          {id}
        </span>
      </td>
      <td className="py-4 pr-6">
        <div className="flex items-center gap-2">
          <StatePill state={s.state} />
          {isDeleted ? <DeletedPill /> : null}
        </div>
      </td>
      <td className="py-4 pr-6 font-mono text-sm tabular-nums text-muted">
        v{s.form_version}
      </td>
      <td className="py-4 pr-6 font-mono text-xs text-muted">
        {formatTimestamp(s.created_at)}
      </td>
      <td className="py-4 pr-6 font-mono text-xs text-muted">
        {s.updated_at ? formatTimestamp(s.updated_at) : "—"}
      </td>
      <td className="py-4 font-mono text-xs text-muted">
        {s.current_step ?? "—"}
      </td>
    </tr>
  );
}

function DeletedPill() {
  return (
    <span
      className="border border-border bg-bg px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.12em] text-muted"
      title="Soft-deleted — data preserved in the database but excluded from default views"
    >
      deleted
    </span>
  );
}

// --- Paginator --------------------------------------------------------------

function Paginator({
  page,
  pageCount,
  rangeStart,
  rangeEnd,
  total,
  onChange,
}: {
  page: number; // 1-indexed
  pageCount: number;
  rangeStart: number;
  rangeEnd: number;
  total: number;
  onChange: (page: number) => void;
}) {
  const canPrev = page > 1;
  const canNext = page < pageCount;
  return (
    <div className="mt-4 flex items-center justify-between gap-4">
      <span className="font-mono text-xs text-muted">
        Showing {rangeStart}–{rangeEnd} of {total}
      </span>
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => onChange(Math.max(1, page - 1))}
          disabled={!canPrev}
          className="font-mono text-xs text-muted hover:text-ink disabled:opacity-30"
        >
          ← Prev
        </button>
        <span className="font-mono text-xs text-ink tabular-nums">
          {page} / {pageCount}
        </span>
        <button
          type="button"
          onClick={() => onChange(Math.min(pageCount, page + 1))}
          disabled={!canNext}
          className="font-mono text-xs text-muted hover:text-ink disabled:opacity-30"
        >
          Next →
        </button>
      </div>
    </div>
  );
}
