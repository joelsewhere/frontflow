/**
 * The two panel kinds a workspace can hold.
 *
 * A form panel reuses the real form-filling pages rather than a
 * reimplementation, so a submission started in a workspace is the same
 * submission as anywhere else — same routes, same state, same history.
 *
 * Those pages are bound to react-router (useParams, useNavigate,
 * useLocation), so each panel gets its own MemoryRouter. The hooks work
 * unchanged, and each panel navigates independently: starting a
 * submission in one panel does not move the browser URL or disturb the
 * others. The routes below mirror the live-form routes in main.tsx —
 * they have to, because the pages navigate between them by path.
 */

import {
  Suspense,
  lazy,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useQuery } from "@tanstack/react-query";

import { DashboardEmbed } from "../components/blocks/DashboardBlock";
import { getWorkspaceExploreTarget } from "../lib/api";
import {
  FormRoutingProvider,
  parseFormPath,
  type FormRoutingValue,
} from "../lib/formRouting";
import { FormThemeProvider } from "../theme/FormThemeProvider";

const LandingPage = lazy(() => import("../pages/LandingPage"));
const SubmissionPage = lazy(() => import("../pages/SubmissionPage"));

function Loading() {
  return <div className="p-4 text-sm text-muted">Loading…</div>;
}

/**
 * A form, filled inside a dock panel.
 *
 * The panel keeps its own path in state and feeds it to the form pages
 * through FormRoutingProvider. A nested <Router> is not an option —
 * react-router v7 refuses to render one inside another — and an iframe
 * is not either: frontflow serves `frame-ancestors 'none'` unless a form
 * opts in via `iframe_allowed_origins`, which only applies to *public*
 * forms. Framing would therefore have forced every workspace form to be
 * published, contradicting the access model.
 *
 * Each panel navigates independently, and the browser URL never moves —
 * which is what you want when four forms are open at once.
 */
/**
 * Reports an element's natural height as it changes.
 *
 * Measures the INNER element, not the scroll container. Under
 * `fit="content"` the panel is what we are trying to size, so its own
 * box is not the answer — and a ResizeObserver on a clipped container
 * never fires anyway, because its border box stays put while the
 * content behind it grows.
 */
function useMeasuredHeight(onMeasure?: (height: number) => void) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const element = ref.current;
    if (!element || !onMeasure) return;

    const observer = new ResizeObserver(() => {
      onMeasure(element.getBoundingClientRect().height);
    });
    observer.observe(element);
    onMeasure(element.getBoundingClientRect().height);

    return () => observer.disconnect();
  }, [onMeasure]);

  return ref;
}

export function WorkspaceFormPanel({
  formId,
  onMeasure,
}: {
  formId: string;
  /** Set when the panel is `fit="content"`: the grid sizes itself to
   *  whatever the form actually turns out to be. */
  onMeasure?: (height: number) => void;
}) {
  const [path, setPath] = useState(`/forms/${encodeURIComponent(formId)}/form`);
  const [state, setState] = useState<unknown>(null);

  const navigate = useCallback(
    (to: string, options?: { replace?: boolean; state?: unknown }) => {
      setPath(to);
      setState(options?.state ?? null);
    },
    [],
  );

  const parsed = useMemo(() => parseFormPath(path), [path]);

  const routing: FormRoutingValue = useMemo(
    () => ({
      // The panel's form always wins: a malformed path must not let a
      // panel start rendering some other form.
      formId,
      submissionId: parsed.submissionId,
      viewId: parsed.viewId,
      state,
      navigate,
    }),
    [formId, parsed.submissionId, parsed.viewId, state, navigate],
  );

  // Landing until the form has been started; the submission view after.
  const started = path.includes("/form/draft") || path.includes("/form/submission");

  const measureRef = useMeasuredHeight(onMeasure);

  return (
    <div className="h-full overflow-auto bg-bg">
      <div ref={measureRef}>
        <Suspense fallback={<Loading />}>
          <FormRoutingProvider value={routing}>
            {/* The end-user views expect the form's own theme tokens, the
                same as on their real routes. */}
            <FormThemeProvider formId={formId}>
              {started ? <SubmissionPage /> : <LandingPage />}
            </FormThemeProvider>
          </FormRoutingProvider>
        </Suspense>
      </div>
    </div>
  );
}

export function WorkspaceDashboardPanel({
  workspaceId,
  name,
  showFilters,
  canEdit,
}: {
  workspaceId: string;
  name: string;
  showFilters: boolean;
  /** From the workspace's `can_edit_dashboards` — offers the Superset
   *  editor. Dashboards only; a form's definition lives in its DSL. */
  canEdit: boolean;
}) {
  return (
    <div className="h-full overflow-hidden bg-bg p-2">
      {/* `fill` rather than a fixed height: a dock panel sizes itself,
          unlike a form layout where the page scrolls and a dashboard
          needs an explicit height. */}
      <DashboardEmbed
        workspaceId={workspaceId}
        formId={null}
        submissionId={null}
        name={name}
        height={0}
        showFilters={showFilters}
        canEdit={canEdit}
        fill
      />
    </div>
  );
}

/**
 * Superset's Explore, as a panel.
 *
 * This is the self-serve surface — a person builds their own view of the
 * data rather than reading a dashboard someone composed. It loads under
 * the viewer's OWN Superset session, because a guest token cannot serve
 * Explore: guest tokens grant dashboards only, and Superset rejects
 * modified query payloads from guest users. There is no framing trick
 * that gets around that, so the panel says what it needs instead of
 * showing an unexplained login screen.
 */
export function WorkspaceExplorePanel({
  workspaceId,
  dataset,
}: {
  workspaceId: string;
  dataset: string | null;
}) {
  const target = useQuery({
    queryKey: ["workspace-explore", workspaceId, dataset],
    queryFn: () => getWorkspaceExploreTarget(workspaceId, dataset),
  });

  if (target.isPending) {
    return <div className="p-4 text-sm text-muted">Loading…</div>;
  }
  if (target.isError) {
    return (
      <div className="m-3 rounded-md border border-error/40 bg-error/10 p-3 text-sm">
        {(target.error as Error).message}
      </div>
    );
  }

  const { superset_domain: domain, dataset_id: datasetId } = target.data;
  // A known dataset opens straight into Explore; an unknown one falls
  // back to Superset's picker rather than a dead frame.
  //
  // `datasource` is the composite `${id}__${type}` form, parsed by the
  // Explore SPA — /explore/ is served by ExploreView.root, which only
  // renders the app template. (`dataset_id` is handled ONLY by the
  // deprecated Flask route in views/core.py, so it binds nothing here.)
  //
  // datasource is placed FIRST on purpose. Superset does not URL-encode
  // the `next` value on a login bounce, so everything after the first &
  // can be lost; putting the dataset first means it is what survives.
  //
  // NO standalone=1 here, unlike the dashboard surfaces. In Explore
  // standalone does not merely hide chrome — ExploreViewContainer has
  //
  //     if (props.standalone) return renderChartContainer();
  //
  // which renders ONLY the chart area: no control panel, no datasource
  // panel, nothing to explore with. It suits embedding a chart preview
  // and defeats interactive exploration entirely. The cost of leaving it
  // off is Superset's nav bar inside the panel.
  //
  // ChartCreation (/chart/add) has no such branch, so standalone there
  // just hides chrome and is safe to keep.
  const url =
    datasetId != null
      ? `${domain}/explore/?datasource=${datasetId}__table&viz_type=table`
      : `${domain}/chart/add?standalone=1`;

  return (
    <div className="flex h-full flex-col bg-bg p-2">
      <div className="mb-1 flex items-center gap-2 text-xs text-muted">
        Exploring as your Superset user —{" "}
        <a
          href={url}
          target="_blank"
          rel="noreferrer"
          className="text-accent underline"
        >
          open in a new tab
        </a>{" "}
        if this frame shows a login screen, or use the panel's reload
        control.
        {datasetId == null && dataset && (
          <> Dataset <code>{dataset}</code> is not registered in Superset yet.</>
        )}
      </div>
      <iframe
        src={url}
        title="Explore in Superset"
        className="min-h-0 w-full flex-1 rounded-md border border-border"
      />
    </div>
  );
}
