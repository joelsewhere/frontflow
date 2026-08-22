/**
 * Which submission a workspace's dashboards should be listening to.
 *
 * Inside a form, a dashboard block gets its submission from the page
 * around it. A workspace has no such page: the form and the dashboard
 * are separate dock panels that know nothing about each other, so the
 * dashboard polled nothing and no refresh or filter directive ever
 * reached it.
 *
 * A form panel publishes its submission here as it starts one; the
 * dashboard panels read it. The directive still rides the poll the form
 * is already doing — react-query serves both from one query key — so
 * this adds a channel, not a transport.
 *
 * Scoped to the browser tab on purpose. A directive says "point THIS
 * person's dashboard at what they just submitted", and routing it
 * through the server would move a colleague's view as a side effect of
 * someone else's submission.
 */

import { createContext, useCallback, useContext, useMemo, useState } from "react";

export interface ActiveSubmission {
  formId: string;
  submissionId: string;
}

interface ActiveSubmissionValue {
  active: ActiveSubmission | null;
  publish: (formId: string, submissionId: string | null) => void;
}

const ActiveSubmissionContext = createContext<ActiveSubmissionValue>({
  active: null,
  publish: () => {},
});

export function useActiveSubmission(): ActiveSubmissionValue {
  return useContext(ActiveSubmissionContext);
}

export function ActiveSubmissionProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [active, setActive] = useState<ActiveSubmission | null>(null);

  const publish = useCallback(
    (formId: string, submissionId: string | null) => {
      setActive((previous) => {
        if (!submissionId) return previous;
        if (
          previous &&
          previous.formId === formId &&
          previous.submissionId === submissionId
        ) {
          return previous;
        }
        // Most recent wins. With several form panels open, the one just
        // worked in is the one a dashboard should be following.
        return { formId, submissionId };
      });
    },
    [],
  );

  const value = useMemo(() => ({ active, publish }), [active, publish]);

  return (
    <ActiveSubmissionContext.Provider value={value}>
      {children}
    </ActiveSubmissionContext.Provider>
  );
}
