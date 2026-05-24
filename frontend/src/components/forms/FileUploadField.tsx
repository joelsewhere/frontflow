import { useRef, useState } from "react";
import { Field } from "./Field";
import { ApiError, type UploadResult } from "../../lib/api";

/**
 * The two value shapes a file field can hold between picks and submits:
 *   - a raw `File` chosen by the user but not yet uploaded;
 *   - an `UploadResult` reference returned from a prior upload (edit-
 *     resume case, or post-submit replay).
 * Uploads happen at form submit, not at pick — the form's submit
 * handler walks the values and uploads any raw `File` before posting.
 */
export type PendingFile = File | UploadResult;

function isUploaded(v: PendingFile | null): v is UploadResult {
  return !!v && typeof (v as UploadResult).kind === "string";
}

function fileLabel(v: PendingFile): { name: string; size: number } {
  if (isUploaded(v)) return { name: v.filename, size: v.size };
  return { name: v.name, size: v.size };
}

interface FileUploadFieldProps {
  label: string;
  formId: string;
  fieldId: string;
  /** The accepted extensions (no dot), e.g. ["pdf", "csv"]. */
  accept: string[];
  maxSizeMb: number;
  error?: string;
  hint?: string;
  /** The current value — a pending `File`, an uploaded reference, or
   *  null. Picks set a `File`; the form's submit handler converts that
   *  to an `UploadResult` via the upload endpoint, then submits. */
  value: PendingFile | null;
  onChange: (value: PendingFile | null) => void;
  /** Current submission id, if any — preserved for compatibility, no
   *  longer used at pick time (the form's submit handler reads it). */
  submissionId?: string | null;
  /** Preserved for compatibility. */
  getDraftValues?: () => Record<string, unknown>;
}

/**
 * Upload control shared by the File and S3File inputs. Click or drop a
 * file; the browser holds it in form state until submit. Size and
 * type are pre-checked here client-side; the server enforces them
 * again at submit-time upload.
 */
export function FileUploadField({
  label,
  fieldId,
  accept,
  maxSizeMb,
  error,
  hint,
  value,
  onChange,
}: FileUploadFieldProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);

  const acceptAttr = accept.length
    ? accept.map((e) => "." + e).join(",")
    : undefined;

  function handleFile(file: File) {
    setLocalError(null);

    // Client-side pre-checks — the server enforces these too on the
    // submit-time upload.
    if (accept.length) {
      const ext = file.name.includes(".")
        ? file.name.split(".").pop()!.toLowerCase()
        : "";
      if (!accept.includes(ext)) {
        setLocalError(
          `That file type isn't accepted. Allowed: ${accept
            .map((e) => "." + e)
            .join(", ")}.`,
        );
        return;
      }
    }
    if (file.size > maxSizeMb * 1024 * 1024) {
      setLocalError(`That file is over the ${maxSizeMb} MB limit.`);
      return;
    }

    // Hold the raw File in form state; the form's submit handler
    // performs the upload before posting the step.
    onChange(file);
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files?.[0];
    if (file) handleFile(file);
  }

  const shownError = error ?? localError ?? undefined;

  return (
    <Field
      label={label}
      htmlFor={fieldId}
      error={shownError}
      hint={hint}
    >
      {value ? (
        (() => {
          const { name, size } = fileLabel(value);
          return (
            <div className="flex items-center gap-3 rounded-theme border border-border bg-surface px-4 py-3">
              <span
                className="min-w-0 flex-1 truncate font-sans text-sm text-ink"
                title={name}
              >
                {name}
              </span>
              <span className="shrink-0 font-mono text-xs text-muted">
                {(size / 1024).toFixed(1)} KB
              </span>
              <button
                type="button"
                onClick={() => {
                  onChange(null);
                  setLocalError(null);
                }}
                className="shrink-0 font-mono text-[10px] uppercase tracking-[0.16em] text-muted hover:text-error"
              >
                Remove
              </button>
            </div>
          );
        })()
      ) : (
        <div
          onClick={() => inputRef.current?.click()}
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
          className={[
            "flex cursor-pointer flex-col items-center justify-center gap-1",
            "rounded-theme border border-dashed px-4 py-8 text-center",
            "transition-colors",
            dragOver
              ? "border-ink bg-surface"
              : "border-border hover:border-muted",
            shownError ? "border-error" : "",
          ].join(" ")}
        >
          <span className="font-sans text-sm text-ink">
            Click or drop a file to upload
          </span>
          <span className="font-mono text-[11px] text-muted">
            {accept.length
              ? accept.map((e) => "." + e).join(" · ")
              : "any file type"}
            {" · up to "}
            {maxSizeMb} MB
          </span>
        </div>
      )}
      <input
        ref={inputRef}
        id={fieldId}
        type="file"
        accept={acceptAttr}
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) handleFile(file);
          e.target.value = ""; // allow re-selecting the same file
        }}
      />
    </Field>
  );
}

// Re-export for convenience — uploadFile and ApiError are needed by
// the form's submit handler, which performs the deferred upload.
export { ApiError };
