import { useEffect, useState, useCallback } from "react";
import {
  getFormVisibility,
  setFormVisibility,
  regenerateUnlistedToken,
  addFormAclUser,
  removeFormAclUser,
  listUsers,
  type FormVisibility,
  type Visibility,
  type AccountInfo,
} from "../../lib/api";

const MODES: { value: Visibility; label: string; hint: string }[] = [
  {
    value: "public",
    label: "Public",
    hint: "Anyone with the link can open and fill this form.",
  },
  {
    value: "unlisted",
    label: "Unlisted",
    hint: "Only people with the private link can reach it.",
  },
  {
    value: "restricted",
    label: "Restricted",
    hint: "Only the signed-in users you list below.",
  },
];

/**
 * The visibility control on a form's summary page. Sets public /
 * unlisted / restricted, surfaces the unlisted share link, and manages
 * the restricted allow-list. Requires manage access (the page already
 * gates that).
 */
export function VisibilityControl({ formId }: { formId: string }) {
  const [vis, setVis] = useState<FormVisibility | null>(null);
  const [users, setUsers] = useState<AccountInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [addUserId, setAddUserId] = useState<number | "">("");

  const load = useCallback(async () => {
    try {
      setVis(await getFormVisibility(formId));
    } catch {
      setError("Couldn't load visibility settings.");
    }
  }, [formId]);

  useEffect(() => {
    load();
    listUsers()
      .then(setUsers)
      .catch(() => {
        /* user list is only needed for restricted mode */
      });
  }, [load]);

  if (error) {
    return <p className="text-sm text-error">{error}</p>;
  }
  if (!vis) {
    return <p className="text-sm text-muted">Loading…</p>;
  }

  async function changeMode(mode: Visibility) {
    setVis(await setFormVisibility(formId, mode));
  }
  async function regen() {
    await regenerateUnlistedToken(formId);
    await load();
  }
  async function addUser() {
    if (addUserId === "") return;
    await addFormAclUser(formId, addUserId);
    setAddUserId("");
    await load();
  }
  async function dropUser(userId: number) {
    await removeFormAclUser(formId, userId);
    await load();
  }

  const shareLink =
    vis.unlisted_token != null
      ? `${window.location.origin}/forms/${encodeURIComponent(
          formId,
        )}/form?key=${vis.unlisted_token}`
      : null;

  const aclIds = new Set(vis.acl.map((u) => u.id));
  const addable = users.filter((u) => !aclIds.has(u.id));

  return (
    <section className="border border-border p-5">
      <h3 className="mb-3 font-mono text-[11px] uppercase tracking-[0.2em] text-muted">
        Visibility
      </h3>

      <div className="flex flex-col gap-2">
        {MODES.map((m) => (
          <button
            key={m.value}
            onClick={() => changeMode(m.value)}
            className={[
              "flex flex-col items-start gap-0.5 border px-4 py-3 text-left transition-colors",
              vis.visibility === m.value
                ? "border-ink bg-surface"
                : "border-border hover:border-muted",
            ].join(" ")}
          >
            <span className="font-sans text-sm uppercase tracking-[0.16em] text-ink">
              {m.label}
            </span>
            <span className="text-xs text-muted">{m.hint}</span>
          </button>
        ))}
      </div>

      {vis.visibility === "unlisted" && shareLink && (
        <div className="mt-4">
          <p className="mb-1 font-mono text-[10px] uppercase tracking-[0.18em] text-muted">
            Private link
          </p>
          <div className="flex gap-2">
            <input
              readOnly
              value={shareLink}
              onFocus={(e) => e.currentTarget.select()}
              className="min-w-0 flex-1 border border-border bg-bg px-3 py-2 font-mono text-xs text-ink"
            />
            <button
              onClick={regen}
              className="shrink-0 border border-border px-3 py-2 font-mono text-[10px] uppercase tracking-[0.18em] text-muted hover:border-ink hover:text-ink"
            >
              Regenerate
            </button>
          </div>
          <p className="mt-1 text-xs text-muted">
            Regenerating invalidates the existing link.
          </p>
        </div>
      )}

      {vis.visibility === "restricted" && (
        <div className="mt-4">
          <p className="mb-2 font-mono text-[10px] uppercase tracking-[0.18em] text-muted">
            Permitted users
          </p>
          <ul className="mb-2 flex flex-col">
            {vis.acl.map((u) => (
              <li
                key={u.id}
                className="flex items-center justify-between border-b border-border py-2"
              >
                <span className="font-mono text-sm text-ink">
                  {u.username}
                </span>
                <button
                  onClick={() => dropUser(u.id)}
                  className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted hover:text-error"
                >
                  Remove
                </button>
              </li>
            ))}
            {vis.acl.length === 0 && (
              <li className="py-2 text-xs text-muted">
                No users yet — only folder-grant holders and admins can
                reach this form.
              </li>
            )}
          </ul>
          <div className="flex gap-2">
            <select
              value={addUserId}
              onChange={(e) =>
                setAddUserId(
                  e.target.value === "" ? "" : Number(e.target.value),
                )
              }
              className="flex-1 border border-border bg-surface px-3 py-2 text-sm text-ink focus:border-ink focus:outline-none"
            >
              <option value="">Add a user…</option>
              {addable.map((u) => (
                <option key={u.id} value={u.id}>
                  {u.username}
                  {u.is_admin ? " (admin)" : ""}
                </option>
              ))}
            </select>
            <button
              onClick={addUser}
              disabled={addUserId === ""}
              className="border border-ink bg-ink px-3 py-2 font-mono text-[10px] uppercase tracking-[0.18em] text-bg hover:opacity-80 disabled:opacity-40"
            >
              Add
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
