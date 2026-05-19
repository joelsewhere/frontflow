import { useRef, useState } from "react";
import { Field } from "./Field";
import { uploadFile, ApiError, type UploadResult } from "../../lib/api";

interface FileUploadFieldProps {
  label: string;
  formId: string;
  fieldId: string;
  /** The accepted extensions (no dot), e.g. ["pdf", "csv"]. */
  accept: string[];
  maxSizeMb: number;
  error?: string;
  hint?: string;
  /** The current value — the upload reference, or null. */
  value: UploadResult | null;
  onChange: (value: UploadResult | null) => void;
  /** Current submission id, if any — lets an S3File key template
   *  resolve earlier-step `steps` references. */
  submissionId?: string | null;
  /** Returns this screen's current field values — lets an S3File key
   *  template resolve same-screen `steps` references. */
  getDraftValues?: () => Record<string, unknown>;
}

/**
 * Upload control shared by the File and S3File inputs. Click or drop a
 * file; it uploads immediately and the returned reference becomes the
 * field value. Size and type are pre-checked here, then enforced again
 * server-side.
 */
export function FileUploadField({
  label,
  formId,
  fieldId,
  accept,
  maxSizeMb,
  error,
  hint,
  value,
  onChange,
  submissionId,
  getDraftValues,
}: FileUploadFieldProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);

  const acceptAttr = accept.length
    ? accept.map((e) => "." + e).join(",")
    : undefined;

  async function handleFile(file: File) {
    setLocalError(null);

    // Client-side pre-checks — the server enforces these too.
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

    setBusy(true);
    try {
      const result = await uploadFile(formId, fieldId, file, {
        submissionId,
        draftValues: getDraftValues?.(),
      });
      onChange(result);
    } catch (err) {
      setLocalError(
        err instanceof ApiError ? err.message : "Upload failed.",
      );
    } finally {
      setBusy(false);
    }
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
        <div className="flex items-center justify-between gap-3 rounded-theme border border-border bg-surface px-4 py-3">
          <span className="min-w-0 truncate font-sans text-sm text-ink">
            {value.filename}
            <span className="ml-2 text-muted">
              {(value.size / 1024).toFixed(1)} KB
            </span>
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
            {busy ? "Uploading…" : "Click or drop a file to upload"}
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
