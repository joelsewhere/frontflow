import { useEffect, type ReactNode } from "react";

interface DrawerProps {
  open: boolean;
  onClose: () => void;
  children: ReactNode;
  /**
   * Disable backdrop click + escape key. Useful while an in-flight
   * mutation is running and we don't want the user to bail out mid-write.
   */
  preventDismiss?: boolean;
}

/**
 * Right-anchored side panel. Full viewport height, ~560px wide on
 * desktop, fills the screen on small viewports. Backdrop click and
 * Escape key close it unless `preventDismiss` is set. Locks body
 * scroll while open; the drawer body scrolls independently.
 *
 * Same a11y caveats as Modal — no focus trap, no aria-modal wiring.
 * Sufficient for the admin-side panel use; swap in a headless dialog
 * primitive if richer accessibility is needed.
 */
export function Drawer({ open, onClose, children, preventDismiss }: DrawerProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !preventDismiss) onClose();
    };
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [open, onClose, preventDismiss]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50">
      <div
        className="absolute inset-0 bg-ink/40"
        onClick={() => !preventDismiss && onClose()}
        aria-hidden
      />
      <aside
        className="absolute right-0 top-0 z-10 flex h-full w-full max-w-[640px] flex-col border-l border-border bg-surface shadow-2xl"
        // Surface is full-height; its content scrolls inside.
      >
        {children}
      </aside>
    </div>
  );
}
