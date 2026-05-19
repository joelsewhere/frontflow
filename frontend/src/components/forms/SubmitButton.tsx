import { type ButtonHTMLAttributes, type ReactNode } from "react";

interface SubmitButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  isLoading?: boolean;
  loadingLabel?: string;
  children: ReactNode;
}

/**
 * Primary submit button. When isLoading is true, disables itself and
 * swaps the label. Visual variant is opinionated and shared across forms.
 */
export function SubmitButton({
  isLoading,
  loadingLabel = "Working…",
  children,
  disabled,
  className,
  ...rest
}: SubmitButtonProps) {
  return (
    <button
      type="submit"
      disabled={disabled || isLoading}
      {...rest}
      className={[
        "group relative inline-flex items-center justify-center gap-2",
        "px-6 py-3 bg-ink text-bg rounded-theme",
        "font-sans text-sm uppercase tracking-[0.18em]",
        "transition-colors duration-200",
        "hover:bg-accent-hover disabled:opacity-50 disabled:cursor-not-allowed",
        className ?? "",
      ].join(" ")}
    >
      {isLoading ? (
        <>
          <Spinner />
          <span>{loadingLabel}</span>
        </>
      ) : (
        <>
          <span>{children}</span>
          <span aria-hidden className="transition-transform group-hover:translate-x-0.5">
            →
          </span>
        </>
      )}
    </button>
  );
}

function Spinner() {
  return (
    <svg
      className="animate-spin h-4 w-4"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden
    >
      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeOpacity="0.25" strokeWidth="2" />
      <path
        d="M22 12a10 10 0 0 1-10 10"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  );
}
