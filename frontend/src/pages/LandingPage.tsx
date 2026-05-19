import { useNavigate, useParams } from "react-router-dom";
import { NodeForm } from "../components/blocks/NodeForm";
import { ApiError } from "../lib/api";
import { useFormDetail } from "../hooks/useFormDetail";
import { useStartSubmission } from "../hooks/useStartSubmission";

/**
 * Landing page for a form. The form_id is in the URL; the title and
 * description come from @form, and the body below them is the landing
 * node's layout tree — rendered by NodeForm. Nothing is hardcoded.
 */
export default function LandingPage() {
  const { formId } = useParams<{ formId: string }>();
  const navigate = useNavigate();
  const { data: form, error: formError, isLoading } = useFormDetail(formId);
  const {
    mutate,
    isPending,
    error: submitError,
  } = useStartSubmission(formId ?? "", (data) => {
    const base = `/forms/${encodeURIComponent(data.form_id)}/form`;
    if (data.submission_id) {
      // Id minted at the first submit — go straight to the live run.
      navigate(
        `${base}/submission/${encodeURIComponent(data.submission_id)}`,
      );
    } else {
      // Still a session draft — no id, no resumable URL. The handle
      // rides in router state, so a refresh genuinely loses it.
      navigate(`${base}/draft`, { state: { handle: data.handle } });
    }
  });

  return (
    <main className="relative z-10 mx-auto max-w-2xl px-6 pt-24 pb-16">
      <header className="mb-14">
        {isLoading ? (
          <h1 className="font-display text-5xl font-bold leading-[1.0] text-ink opacity-30">
            Loading…
          </h1>
        ) : formError ? (
          <h1 className="font-display text-5xl font-bold leading-[1.0] text-ink">
            {formError instanceof ApiError && formError.status === 404
              ? "Form not found"
              : "Couldn't load form"}
          </h1>
        ) : form ? (
          <>
            <h1
              className="font-display text-5xl font-bold leading-[1.0]"
              style={{ color: "rgb(var(--form-title-color))" }}
            >
              {form.title}
            </h1>
            {form.description ? (
              <p className="mt-6 font-sans text-muted text-base max-w-md leading-relaxed">
                {form.description}
              </p>
            ) : null}
          </>
        ) : null}
      </header>

      {formError ? (
        <div className="border border-error bg-surface p-6">
          <p className="text-error text-sm">
            Couldn't load form: {formError.message}
          </p>
        </div>
      ) : null}

      {form ? (
        <NodeForm
          layout={form.landing_step.layout}
          nodeId={form.landing_step.step_id}
          formId={formId!}
          submissionId={null}
          isSubmitting={isPending}
          error={submitError?.message ?? null}
          onSubmit={(values) => mutate(values)}
        />
      ) : null}
    </main>
  );
}
