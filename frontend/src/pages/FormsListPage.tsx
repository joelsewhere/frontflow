import { useMemo, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, type FormSummary } from "../lib/api";
import { formatTimestamp } from "../lib/format";
import { useFormsList } from "../hooks/useFormsList";
import { SignOutControl } from "../components/console/SignOutControl";
import { useAuth } from "../auth/AuthContext";

/**
 * The forms index (`/forms`) — every registered form, grouped by the
 * folder its DSL file lives in, with submission counts and tracking
 * metadata. A form links to its submission list.
 */
export default function FormsListPage() {
  const { user } = useAuth();
  const { data: forms, error, isLoading } = useFormsList();
  const groups = useMemo(
    () => (forms ? groupByFolder(forms) : []),
    [forms],
  );

  return (
    <main className="relative z-10 mx-auto max-w-7xl px-6 pt-24 pb-16">
      <header className="mb-12 flex items-end justify-between gap-6">
        <div>
          <h1 className="font-display text-5xl font-bold leading-[1.0] text-ink">
            Forms
          </h1>
          {forms ? (
            <p className="mt-4 font-mono text-xs uppercase tracking-wider text-muted">
              {forms.length} {forms.length === 1 ? "form" : "forms"}
            </p>
          ) : null}
        </div>
        <div className="flex shrink-0 items-center gap-6">
          {user?.is_admin && (
            <>
              <Link
                to="/users"
                className="font-mono text-xs uppercase tracking-wider text-muted hover:text-accent"
              >
                Users →
              </Link>
              <Link
                to="/access"
                className="font-mono text-xs uppercase tracking-wider text-muted hover:text-accent"
              >
                Access →
              </Link>
              <Link
                to="/connections"
                className="font-mono text-xs uppercase tracking-wider text-muted hover:text-accent"
              >
                Connections →
              </Link>
            </>
          )}
          <SignOutControl />
        </div>
      </header>

      {isLoading ? (
        <p className="font-display text-2xl text-ink opacity-30">Loading…</p>
      ) : error ? (
        <div className="border border-error bg-surface p-6">
          <p className="text-error text-sm">
            Couldn't load forms:{" "}
            {error instanceof ApiError ? error.message : "unknown error"}
          </p>
        </div>
      ) : forms && forms.length === 0 ? (
        <p className="text-muted text-sm">No forms are registered yet.</p>
      ) : (
        <div className="space-y-12">
          {groups.map((group) => (
            <FolderGroup
              key={group.folder || "__root__"}
              folder={group.folder}
              items={group.items}
            />
          ))}
        </div>
      )}
    </main>
  );
}

/** Group forms by folder, preserving the backend's (folder, id) order. */
function groupByFolder(
  forms: FormSummary[],
): { folder: string; items: FormSummary[] }[] {
  const byFolder = new Map<string, FormSummary[]>();
  for (const f of forms) {
    const existing = byFolder.get(f.folder_path);
    if (existing) existing.push(f);
    else byFolder.set(f.folder_path, [f]);
  }
  return [...byFolder.entries()].map(([folder, items]) => ({ folder, items }));
}

function FolderGroup({
  folder,
  items,
}: {
  folder: string;
  items: FormSummary[];
}) {
  return (
    <section>
      {folder ? (
        <h2 className="mb-3 font-mono text-xs uppercase tracking-[0.15em] text-muted">
          {folder.split("/").join(" / ")}
        </h2>
      ) : null}
      <div className="overflow-x-auto">
        <table className="w-full border-collapse">
          <thead>
            <tr className="border-b border-border">
              <Th>Form</Th>
              <Th>Submissions</Th>
              <Th>Versions</Th>
              <Th>Last activity</Th>
            </tr>
          </thead>
          <tbody>
            {items.map((form) => (
              <FormRow key={form.form_id} form={form} />
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Th({ children }: { children: ReactNode }) {
  return (
    <th className="py-2 pr-6 text-left font-mono text-[11px] font-medium uppercase tracking-wider text-muted">
      {children}
    </th>
  );
}

function FormRow({ form }: { form: FormSummary }) {
  const navigate = useNavigate();
  const to = `/forms/${encodeURIComponent(form.form_id)}`;
  const { running, success, failed, total } = form.submissions;

  return (
    <tr
      onClick={() => navigate(to)}
      className="cursor-pointer border-b border-border transition-colors hover:bg-surface"
    >
      <td className="py-4 pr-6 align-top">
        <Link
          to={to}
          onClick={(e) => e.stopPropagation()}
          className="font-medium text-ink hover:text-accent"
        >
          {form.name}
        </Link>
        <div className="mt-1 flex items-center gap-2">
          <span className="font-mono text-xs text-muted">{form.form_id}</span>
          {!form.is_live ? (
            <span className="border border-border px-1.5 font-mono text-[10px] uppercase tracking-wider text-muted">
              Archived
            </span>
          ) : null}
        </div>
      </td>
      <td className="py-4 pr-6 align-top">
        {total === 0 ? (
          <span className="text-muted">—</span>
        ) : (
          <div>
            <div className="tabular-nums text-ink">{total}</div>
            <div className="mt-0.5 flex gap-2 font-mono text-[11px]">
              {running > 0 ? (
                <span className="text-accent">{running} running</span>
              ) : null}
              {success > 0 ? (
                <span className="text-muted">{success} done</span>
              ) : null}
              {failed > 0 ? (
                <span className="text-error">{failed} failed</span>
              ) : null}
            </div>
          </div>
        )}
      </td>
      <td className="py-4 pr-6 align-top font-mono text-sm tabular-nums text-ink">
        {form.version_count}
      </td>
      <td className="py-4 align-top font-mono text-xs text-muted">
        {formatTimestamp(form.last_activity)}
      </td>
    </tr>
  );
}
