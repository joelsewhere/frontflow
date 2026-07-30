import { useEffect, type ReactNode } from "react";

interface ModalProps {
  open: boolean;
  onClose: () => void;
  children: ReactNode;
  /**
   * Disable backdrop click + escape key. Useful while an in-flight
   * mutation is running and we don't want the user to bail out mid-write.
   */
  preventDismiss?: boolean;
  /**
   * Tailwind width-cap class for the modal surface. Defaults to
   * `max-w-md` — fine for confirmations and short forms, too narrow
   * for content-heavy modals (side-by-side diffs, wide tables) which
   * should override (e.g. `max-w-[110rem]` for a near-fullscreen
   * surface that still feels framed on a wide monitor).
   */
  widthClass?: string;
}

/**
 * Minimal modal. Renders a fixed-position backdrop and a centered
 * surface. Escape key and backdrop clicks close it unless
 * preventDismiss is set. Locks body scroll while open.
 *
 * Not a full a11y dialog — no focus trap, no aria-modal wiring. Good
 * enough for this app's confirmation flows; swap in a headless dialog
 * primitive (Radix, Headless UI) if richer accessibility is needed.
 */
export function Modal({
  open, onClose, children, preventDismiss,
  widthClass = "max-w-md",
}: ModalProps) {
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
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div
        className="absolute inset-0 bg-ink/40"
        onClick={() => !preventDismiss && onClose()}
        aria-hidden
      />
      <div className={`relative z-10 bg-surface border border-border p-6 ${widthClass} w-full shadow-lg`}>
        {children}
      </div>
    </div>
  );
}
