import { createContext, useContext } from "react";

/**
 * Render mode for the block tree.
 *   - "form":      inputs are editable; buttons submit.
 *   - "submitted": inputs show their submitted value read-only; the
 *                  clicked button shows as chosen.
 */
export type BlockMode = "form" | "submitted";

/**
 * Carried down the block tree so leaves know how to render and (in
 * submitted mode) what the user entered.
 */
export interface BlockRenderContextValue {
  mode: BlockMode;
  /** Locked presentation: the tree renders with its full form-mode
   *  layout — the authored visual composition intact — but every
   *  interaction is disabled. Used for submitted nodes, where the
   *  seeded read-only form context supplies the submitted values.
   *  Buttons render as the chosen-chip; custom-interaction widgets
   *  block their pointer handlers. */
  locked?: boolean;
  /** Submitted field values, keyed by field id. Present in "submitted". */
  values: Record<string, unknown>;
  /** The button id the user clicked. Present in "submitted". */
  clickedButton: string | null;
  /** Id of the node whose layout is being rendered. Used to resolve
   *  same-node `{{ steps.<thisNode>.<field> }}` templates live. */
  nodeId: string;
  /** Id of the form being rendered — used by file-upload fields to
   *  target the upload endpoint. */
  formId: string;
  /** Id of the current submission, if one exists — used by S3File
   *  key templates to resolve earlier-step `steps` references. Null
   *  on the landing screen, before a submission is created. */
  submissionId: string | null;
}

export const BlockRenderContext = createContext<BlockRenderContextValue>({
  mode: "form",
  values: {},
  clickedButton: null,
  nodeId: "",
  formId: "",
  submissionId: null,
});

export function useBlockRender(): BlockRenderContextValue {
  return useContext(BlockRenderContext);
}

/**
 * Provided by NodeForm in form mode. Button blocks consume it to
 * trigger submission (validation runs first; the clicked button's id
 * is sent with the values).
 */
export interface NodeFormContextValue {
  /** Validate the form, then submit with the given button id. */
  submitWith: (buttonId: string | null) => void;
  /** The button currently mid-submit (for per-button loading state). */
  pendingButton: string | null;
  /** True while the submit mutation is in flight. */
  isSubmitting: boolean;
  /** Which phase of the submit pipeline the form is currently in.
   *  `idle` — nothing in flight; `uploading` — deferred file uploads
   *  underway before posting the step; `submitting` — the step is
   *  posting. Lets the active button render an accurate label. */
  uploadPhase: "idle" | "uploading" | "submitting";
}

export const NodeFormContext = createContext<NodeFormContextValue | null>(null);

export function useNodeForm(): NodeFormContextValue | null {
  return useContext(NodeFormContext);
}
