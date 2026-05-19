import { type NodeStatus } from "../../lib/dagState";

interface StatusIndicatorProps {
  status: NodeStatus;
  size?: "sm" | "md";
  label?: string;
}

const SIZE_PX = { sm: 16, md: 24 } as const;

/**
 * Visual indicator for a node status. SVG-based so the visuals stay crisp
 * at any size and animations are CSS-driven.
 *
 * - `pending`: hollow circle, faint
 * - `running`: rotating arc
 * - `success`: filled disc with checkmark
 * - `failed` : filled disc with X
 * - `waiting`: pulsing ring (for HITL deferred — used from step 5)
 */
export function StatusIndicator({
  status,
  size = "md",
  label,
}: StatusIndicatorProps) {
  const px = SIZE_PX[size];
  return (
    <span
      role="img"
      aria-label={label ?? status}
      className="inline-flex items-center justify-center shrink-0"
      style={{ width: px, height: px }}
    >
      {status === "pending" && <PendingIcon px={px} />}
      {status === "running" && <RunningIcon px={px} />}
      {status === "success" && <SuccessIcon px={px} />}
      {status === "failed" && <FailedIcon px={px} />}
      {status === "waiting" && <WaitingIcon px={px} />}
    </span>
  );
}

function PendingIcon({ px }: { px: number }) {
  return (
    <svg width={px} height={px} viewBox="0 0 24 24" fill="none">
      <circle
        cx="12"
        cy="12"
        r="9"
        stroke="currentColor"
        strokeOpacity="0.25"
        strokeWidth="2"
        strokeDasharray="2 3"
      />
    </svg>
  );
}

function RunningIcon({ px }: { px: number }) {
  return (
    <svg
      width={px}
      height={px}
      viewBox="0 0 24 24"
      fill="none"
      className="text-accent"
      style={{ animation: "dag-spin 1.4s linear infinite" }}
    >
      <circle
        cx="12"
        cy="12"
        r="9"
        stroke="currentColor"
        strokeOpacity="0.2"
        strokeWidth="2"
      />
      <circle
        cx="12"
        cy="12"
        r="9"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeDasharray="14 60"
      />
    </svg>
  );
}

function SuccessIcon({ px }: { px: number }) {
  return (
    <svg
      width={px}
      height={px}
      viewBox="0 0 24 24"
      fill="none"
      className="text-ink"
    >
      <circle cx="12" cy="12" r="9" fill="currentColor" />
      <path
        d="M8 12.5l2.6 2.6L16 9.5"
        stroke="#F8F7F4"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function FailedIcon({ px }: { px: number }) {
  return (
    <svg
      width={px}
      height={px}
      viewBox="0 0 24 24"
      fill="none"
      className="text-error"
    >
      <circle cx="12" cy="12" r="9" fill="currentColor" />
      <path
        d="M9 9l6 6M15 9l-6 6"
        stroke="#F8F7F4"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  );
}

function WaitingIcon({ px }: { px: number }) {
  return (
    <svg
      width={px}
      height={px}
      viewBox="0 0 24 24"
      fill="none"
      className="text-accent"
      style={{ animation: "dag-pulse 1.8s ease-in-out infinite" }}
    >
      <circle
        cx="12"
        cy="12"
        r="9"
        stroke="currentColor"
        strokeWidth="2"
      />
      <circle cx="12" cy="12" r="2.5" fill="currentColor" />
    </svg>
  );
}
