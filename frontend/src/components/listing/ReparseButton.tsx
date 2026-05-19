import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { refreshForm } from "../../lib/api";

/** Reparses a single form's workflow source — picking up edits to the
 *  workflow file without a server restart. Shown on the form summary
 *  page; the same action is available to CI/CD via
 *  POST /api/forms/{id}/refresh. */
export function ReparseButton({ formId }: { formId: string }) {
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<
    { ok: true } | { ok: false; message: string } | null
  >(null);

  async function onReparse() {
    setBusy(true);
    setResult(null);
    try {
      const r = await refreshForm(formId);
      if (r.status === "error") {
        setResult({ ok: false, message: r.error ?? "reparse failed" });
      } else {
        setResult({ ok: true });
        // The form may have changed — refetch its detail and graph.
        await queryClient.invalidateQueries({ queryKey: ["formsList"] });
        await queryClient.invalidateQueries({
          queryKey: ["formDetail", formId],
        });
        await queryClient.invalidateQueries({
          queryKey: ["formGraph", formId],
        });
      }
    } catch (e) {
      setResult({
        ok: false,
        message: e instanceof Error ? e.message : "reparse failed",
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex items-center gap-3">
      <button
        onClick={onReparse}
        disabled={busy}
        className="border border-border px-3 py-2 font-mono text-[11px]
          uppercase tracking-[0.2em] text-muted transition-colors
          hover:border-ink hover:text-ink disabled:cursor-not-allowed
          disabled:opacity-40"
      >
        {busy ? "Reparsing…" : "Reparse form"}
      </button>
      {result?.ok === true && (
        <span className="font-mono text-[11px] text-accent">
          Reparsed
        </span>
      )}
      {result?.ok === false && (
        <span className="font-mono text-[11px] text-error">
          {result.message}
        </span>
      )}
    </div>
  );
}
