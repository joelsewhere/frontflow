import { useEffect, useState } from "react";

/**
 * Each option the picker resolved for a given form / node / input.
 * `value` is the identifier the backend will store (the picker's
 * `identifier_kind` decides whether it's a number or a string).
 * `label` is the resolved display string the user sees.
 */
export interface PickerOption {
  value: string | number;
  label: string;
}

interface PickerOptionsState {
  options: PickerOption[];
  loading: boolean;
  /** Plain string message — already user-facing. Null while loading
   *  and on success. */
  error: string | null;
}

/**
 * Resolve a picker input's option list at render time.
 *
 * Hits `GET /api/forms/{form_id}/pickers/{node_id}/{input_id}/options`
 * — the same endpoint the live submission renderer uses. Forwards
 * `token` and `key` query params straight through from `window.location`
 * so token-link visitors and signed embed forms resolve the same way
 * the form-render endpoints do.
 *
 * Re-fetches when any of the three ids change. Bails its setState if
 * the component unmounts (or the deps change) mid-flight — picker
 * fields are commonly mounted inside conditional branches and
 * unmount on every re-render of a `When` arm.
 */
export function usePickerOptions(
  formId: string,
  nodeId: string,
  inputId: string,
): PickerOptionsState {
  const [state, setState] = useState<PickerOptionsState>({
    options: [],
    loading: true,
    error: null,
  });

  useEffect(() => {
    let cancelled = false;
    setState((s) => ({ ...s, loading: true, error: null }));

    const qs = new URLSearchParams();
    const here = new URLSearchParams(window.location.search);
    const token = here.get("token");
    const key = here.get("key");
    if (token) qs.set("token", token);
    if (key) qs.set("key", key);
    const tail = qs.toString();
    const url =
      `/api/forms/${encodeURIComponent(formId)}` +
      `/pickers/${encodeURIComponent(nodeId)}` +
      `/${encodeURIComponent(inputId)}/options` +
      (tail ? `?${tail}` : "");

    fetch(url, { credentials: "same-origin" })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((body) => {
        if (cancelled) return;
        const options: PickerOption[] = Array.isArray(body?.options)
          ? body.options
          : [];
        setState({ options, loading: false, error: null });
      })
      .catch((err) => {
        if (cancelled) return;
        setState({
          options: [],
          loading: false,
          error: String(err?.message ?? err),
        });
      });

    return () => {
      cancelled = true;
    };
  }, [formId, nodeId, inputId]);

  return state;
}
