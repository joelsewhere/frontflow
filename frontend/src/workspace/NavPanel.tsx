/**
 * A workspace's navigation, rendered as an ordinary dock panel.
 *
 * There is no navigation vocabulary in the DSL — a nav holds the same
 * display blocks a form node does, rendered by the same recursive
 * registry. That is what makes `displays.KPI` in a sidebar work without
 * anyone having written a sidebar KPI: it is not a special case, it is
 * the same block.
 *
 * The author's develop-mode switch lives down here rather than in the
 * workspace header, and this is the point of it. A control in the header
 * is chrome an end user never sees, so a workspace showing it is not
 * showing what the end user gets. Tucked into the nav — the way Looker
 * keeps its Development Mode toggle in the sidebar — the panels
 * themselves are exactly what a viewer sees, and the nav collapses to a
 * spine when the author wants it out of the way entirely.
 */

import { useEffect } from "react";
import { FormProvider, useForm } from "react-hook-form";

import { BlockTree } from "../components/blocks/BlockTree";
import { BlockRenderContext } from "../components/blocks/types";
import type { Block, WorkspaceNav } from "../lib/api";

export interface NavPanelProps {
  nav: WorkspaceNav;
  /** Whether this person may edit the workspace's dashboards at all.
   *  The switch below is only offered to them; for anyone else there is
   *  nothing to hide and no switch. */
  canManage: boolean;
  authorTools: boolean;
  onToggleAuthorTools: () => void;
}

export function WorkspaceNavPanel({
  nav,
  canManage,
  authorTools,
  onToggleAuthorTools,
}: NavPanelProps) {
  // Blocks reach for a form context — `When` visibility and label
  // templating both call useWatch(). A nav belongs to the workspace
  // rather than to any submission, so it gets an empty one: the blocks
  // resolve uniformly, against no values.
  const methods = useForm({ defaultValues: {} });
  useEffect(() => {
    methods.reset({});
  }, [methods]);

  const horizontal = nav.kind === "navbar";

  return (
    <div className="flex h-full flex-col overflow-auto bg-surface">
      <div
        className={
          horizontal
            ? "flex flex-1 flex-wrap items-center gap-4 p-3"
            : "flex-1 space-y-3 p-3"
        }
      >
        <FormProvider {...methods}>
          <BlockRenderContext.Provider
            value={{
              mode: "form",
              values: {},
              clickedButton: null,
              // No node and no submission: a nav is workspace chrome,
              // not part of anyone's filling-in of a form.
              nodeId: `<${nav.kind}>`,
              formId: "",
              submissionId: null,
            }}
          >
            {nav.children.map((child, index) => (
              <BlockTree
                key={child.id ?? `${child.type}-${index}`}
                block={child as Block}
              />
            ))}
          </BlockRenderContext.Provider>
        </FormProvider>
      </div>

      {canManage && (
        <div
          className={
            horizontal
              ? "flex items-center border-l border-border pl-3"
              : "border-t border-border p-3"
          }
        >
          <DevelopSwitch on={authorTools} onToggle={onToggleAuthorTools} />
        </div>
      )}
    </div>
  );
}

/**
 * Develop mode.
 *
 * Off, the workspace is exactly what a viewer gets — no dashboard
 * editing controls anywhere. It can only ever subtract: the server
 * decides who may edit, and switching it on reveals nothing the
 * workspace's ACL withheld.
 */
function DevelopSwitch({
  on,
  onToggle,
}: {
  on: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      onClick={onToggle}
      className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs text-muted hover:bg-bg hover:text-ink"
      title={
        on
          ? "Develop mode is on — dashboard editing controls are showing"
          : "Develop mode is off — the workspace looks exactly as a viewer sees it"
      }
    >
      <span
        aria-hidden
        className={`inline-flex h-4 w-7 flex-shrink-0 items-center rounded-full p-0.5 transition-colors ${
          on ? "bg-accent" : "bg-border"
        }`}
      >
        <span
          className={`h-3 w-3 rounded-full bg-bg transition-transform ${
            on ? "translate-x-3" : "translate-x-0"
          }`}
        />
      </span>
      <span className="whitespace-nowrap">Develop mode</span>
    </button>
  );
}
