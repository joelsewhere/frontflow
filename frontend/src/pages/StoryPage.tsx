/**
 * One data story, on its own page.
 *
 * The same framed document the workspace panel shows, with the console's
 * chrome around it — so a story can be linked from the index, from the
 * side navigation, or shared as a URL, rather than existing only as a
 * panel inside one workspace.
 *
 * The story itself is an isolated document: see WorkspaceStoryPanel and
 * STORY_SANDBOX. Nothing on this page can read into it, and nothing in
 * it can read out.
 */

import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { getStory } from "../lib/api";
import { WorkspaceStoryPanel } from "../workspace/panels";

export default function StoryPage() {
  // A story's name is a path within the source tree, so the route is a
  // splat rather than a single parameter.
  const params = useParams();
  const name = params["*"] ?? "";

  const story = useQuery({
    queryKey: ["story", name],
    queryFn: () => getStory(name),
    enabled: Boolean(name),
  });

  return (
    <main className="relative z-10 mx-auto flex h-screen max-w-5xl flex-col px-6 pt-10 pb-6">
      <header className="mb-4 flex items-baseline justify-between gap-6">
        <div className="min-w-0">
          <h1 className="font-display truncate text-3xl font-bold text-ink">
            {story.data?.title ?? name.split("/").pop()}
          </h1>
          <p className="mt-1 font-mono text-xs text-muted">{name}</p>
        </div>
        <Link
          to="/"
          className="shrink-0 font-mono text-xs uppercase tracking-wider text-muted hover:text-accent"
        >
          ← Index
        </Link>
      </header>

      <div className="min-h-0 flex-1 border border-border">
        <WorkspaceStoryPanel name={name} />
      </div>
    </main>
  );
}
