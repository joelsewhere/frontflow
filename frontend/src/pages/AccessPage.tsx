import { useEffect, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import {
  listGroups,
  createGroup,
  getGroup,
  deleteGroup,
  addGroupMember,
  removeGroupMember,
  addGroupGrant,
  removeGrant,
  listUsers,
  type GroupSummary,
  type GroupDetail,
  type AccountInfo,
} from "../lib/api";

/**
 * Access management (`/access`, admin only) — create groups, manage
 * membership, and grant groups roles on folder subtrees. A grant
 * cascades to every form under its folder path.
 */
export default function AccessPage() {
  const [groups, setGroups] = useState<GroupSummary[] | null>(null);
  const [users, setUsers] = useState<AccountInfo[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [newGroup, setNewGroup] = useState("");
  const [error, setError] = useState<string | null>(null);

  const loadGroups = useCallback(async () => {
    try {
      setGroups(await listGroups());
    } catch {
      setError("Couldn't load groups.");
    }
  }, []);

  useEffect(() => {
    loadGroups();
    listUsers()
      .then(setUsers)
      .catch(() => setError("Couldn't load users."));
  }, [loadGroups]);

  async function onCreate() {
    const name = newGroup.trim();
    if (!name) return;
    try {
      await createGroup(name);
      setNewGroup("");
      await loadGroups();
    } catch {
      setError(`Couldn't create group "${name}" — name may be taken.`);
    }
  }

  return (
    <main className="relative z-10 mx-auto max-w-7xl px-6 pt-24 pb-16">
      <header className="mb-10">
        <Link
          to="/forms"
          className="font-mono text-xs uppercase tracking-wider text-muted hover:text-accent"
        >
          ← Forms
        </Link>
        <h1 className="mt-4 font-display text-5xl font-bold text-ink">
          Access
        </h1>
        <p className="mt-3 max-w-xl text-sm text-muted">
          Groups grant their members a role on a folder. A grant
          cascades to every form under that folder path — view to see
          and track, manage to also edit.
        </p>
      </header>

      {error && (
        <div className="mb-6 border border-error bg-surface p-4">
          <p className="text-sm text-error">{error}</p>
        </div>
      )}

      <div className="flex gap-8">
        {/* Group list */}
        <div className="w-72 shrink-0">
          <div className="mb-3 flex gap-2">
            <input
              value={newGroup}
              onChange={(e) => setNewGroup(e.target.value)}
              placeholder="New group name"
              className="min-w-0 flex-1 border border-border bg-surface px-3 py-2 text-sm text-ink focus:border-ink focus:outline-none"
            />
            <button
              onClick={onCreate}
              className="border border-ink bg-ink px-3 py-2 font-mono text-[10px] uppercase tracking-[0.18em] text-bg hover:opacity-80"
            >
              Add
            </button>
          </div>
          <ul className="flex flex-col">
            {groups?.map((g) => (
              <li key={g.id}>
                <button
                  onClick={() => setSelected(g.id)}
                  className={[
                    "flex w-full items-center justify-between border-b border-border px-3 py-3 text-left transition-colors hover:bg-surface",
                    selected === g.id ? "bg-surface" : "",
                  ].join(" ")}
                >
                  <span className="font-mono text-sm text-ink">
                    {g.name}
                  </span>
                  <span className="font-mono text-[10px] text-muted">
                    {g.member_count}m · {g.grant_count}g
                  </span>
                </button>
              </li>
            ))}
            {groups && groups.length === 0 && (
              <li className="py-3 text-sm text-muted">
                No groups yet.
              </li>
            )}
          </ul>
        </div>

        {/* Group detail */}
        <div className="flex-1">
          {selected === null ? (
            <p className="text-sm text-muted">
              Select a group to manage its members and grants.
            </p>
          ) : (
            <GroupPanel
              key={selected}
              groupId={selected}
              users={users}
              onChanged={loadGroups}
              onDeleted={() => {
                setSelected(null);
                loadGroups();
              }}
            />
          )}
        </div>
      </div>
    </main>
  );
}

function GroupPanel({
  groupId,
  users,
  onChanged,
  onDeleted,
}: {
  groupId: number;
  users: AccountInfo[];
  onChanged: () => void;
  onDeleted: () => void;
}) {
  const [detail, setDetail] = useState<GroupDetail | null>(null);
  const [grantPath, setGrantPath] = useState("");
  const [grantRole, setGrantRole] = useState<"view" | "manage">("view");
  const [addUserId, setAddUserId] = useState<number | "">("");

  const load = useCallback(async () => {
    setDetail(await getGroup(groupId));
  }, [groupId]);

  useEffect(() => {
    load();
  }, [load]);

  if (!detail) {
    return <p className="text-sm text-muted">Loading…</p>;
  }

  const memberIds = new Set(detail.members.map((m) => m.id));
  const addableUsers = users.filter((u) => !memberIds.has(u.id));

  async function addMember() {
    if (addUserId === "") return;
    await addGroupMember(groupId, addUserId);
    setAddUserId("");
    await load();
    onChanged();
  }
  async function dropMember(userId: number) {
    await removeGroupMember(groupId, userId);
    await load();
    onChanged();
  }
  async function addGrant() {
    await addGroupGrant(groupId, grantPath.trim(), grantRole);
    setGrantPath("");
    await load();
    onChanged();
  }
  async function dropGrant(grantId: number) {
    await removeGrant(grantId);
    await load();
    onChanged();
  }
  async function onDelete() {
    const groupName = detail?.name ?? "this group";
    if (
      !window.confirm(
        `Delete group "${groupName}"? Its members lose this access.`,
      )
    )
      return;
    await deleteGroup(groupId);
    onDeleted();
  }

  return (
    <div>
      <div className="mb-6 flex items-center justify-between">
        <h2 className="font-display text-2xl font-bold text-ink">
          {detail.name}
        </h2>
        <button
          onClick={onDelete}
          className="border border-error px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.18em] text-error hover:bg-error hover:text-bg"
        >
          Delete group
        </button>
      </div>

      {/* Members */}
      <section className="mb-8">
        <h3 className="mb-3 font-mono text-[11px] uppercase tracking-[0.2em] text-muted">
          Members
        </h3>
        <ul className="mb-3 flex flex-col">
          {detail.members.map((m) => (
            <li
              key={m.id}
              className="flex items-center justify-between border-b border-border py-2"
            >
              <span className="font-mono text-sm text-ink">
                {m.username}
              </span>
              <button
                onClick={() => dropMember(m.id)}
                className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted hover:text-error"
              >
                Remove
              </button>
            </li>
          ))}
          {detail.members.length === 0 && (
            <li className="py-2 text-sm text-muted">No members.</li>
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
            {addableUsers.map((u) => (
              <option key={u.id} value={u.id}>
                {u.username}
                {u.is_admin ? " (admin)" : ""}
              </option>
            ))}
          </select>
          <button
            onClick={addMember}
            disabled={addUserId === ""}
            className="border border-ink bg-ink px-3 py-2 font-mono text-[10px] uppercase tracking-[0.18em] text-bg hover:opacity-80 disabled:opacity-40"
          >
            Add
          </button>
        </div>
      </section>

      {/* Grants */}
      <section>
        <h3 className="mb-3 font-mono text-[11px] uppercase tracking-[0.2em] text-muted">
          Folder grants
        </h3>
        <ul className="mb-3 flex flex-col">
          {detail.grants.map((g) => (
            <li
              key={g.id}
              className="flex items-center justify-between border-b border-border py-2"
            >
              <span className="font-mono text-sm text-ink">
                {g.folder_path === "" ? (
                  <span className="text-muted">/ (all forms)</span>
                ) : (
                  g.folder_path
                )}
                <span className="ml-3 font-mono text-[10px] uppercase tracking-[0.18em] text-accent">
                  {g.role}
                </span>
              </span>
              <button
                onClick={() => dropGrant(g.id)}
                className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted hover:text-error"
              >
                Remove
              </button>
            </li>
          ))}
          {detail.grants.length === 0 && (
            <li className="py-2 text-sm text-muted">No grants.</li>
          )}
        </ul>
        <div className="flex gap-2">
          <input
            value={grantPath}
            onChange={(e) => setGrantPath(e.target.value)}
            placeholder="Folder path (blank = all forms)"
            className="min-w-0 flex-1 border border-border bg-surface px-3 py-2 text-sm text-ink focus:border-ink focus:outline-none"
          />
          <select
            value={grantRole}
            onChange={(e) =>
              setGrantRole(e.target.value as "view" | "manage")
            }
            className="border border-border bg-surface px-3 py-2 text-sm text-ink focus:border-ink focus:outline-none"
          >
            <option value="view">view</option>
            <option value="manage">manage</option>
          </select>
          <button
            onClick={addGrant}
            className="border border-ink bg-ink px-3 py-2 font-mono text-[10px] uppercase tracking-[0.18em] text-bg hover:opacity-80"
          >
            Grant
          </button>
        </div>
      </section>
    </div>
  );
}
