import { useEffect, useRef, useState } from "react";
import { Field } from "./Field";

interface MultiSelectFieldProps {
  label: string;
  options: readonly string[];
  value: string[] | undefined;
  onChange: (v: string[]) => void;
  error?: string;
  hint?: string;
}

/**
 * Search-as-you-type multi-select. A dropdown you type into to filter
 * the options; click an option to add it, and selected options show as
 * removable chips. Controlled — drive it with react-hook-form's
 * <Controller>. Value: a list of option strings.
 */
export function MultiSelectField({
  label,
  options,
  value,
  onChange,
  error, hint,
}: MultiSelectFieldProps) {
  const selected = value ?? [];
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Close the dropdown on an outside click.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const available = options.filter(
    (o) =>
      !selected.includes(o) &&
      o.toLowerCase().includes(query.trim().toLowerCase()),
  );

  const add = (o: string) => {
    onChange([...selected, o]);
    setQuery("");
    inputRef.current?.focus();
  };
  const remove = (o: string) => onChange(selected.filter((s) => s !== o));

  return (
    <Field label={label} error={error} hint={hint}>
      <div ref={wrapRef} className="relative">
        <div
          onClick={() => {
            setOpen(true);
            inputRef.current?.focus();
          }}
          className={[
            "flex min-h-[3rem] flex-wrap items-center gap-1.5",
            "cursor-text rounded-theme border bg-surface px-2 py-2",
            "transition-colors",
            error ? "border-error" : "border-border focus-within:border-ink",
          ].join(" ")}
        >
          {selected.map((s) => (
            <span
              key={s}
              className="inline-flex items-center gap-1.5 border border-border bg-bg px-2 py-1 text-sm text-ink"
            >
              {s}
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  remove(s);
                }}
                className="text-muted transition-colors hover:text-error"
                aria-label={`Remove ${s}`}
              >
                ×
              </button>
            </span>
          ))}
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setOpen(true);
            }}
            onFocus={() => setOpen(true)}
            placeholder={selected.length === 0 ? "Search options…" : ""}
            className="min-w-[7rem] flex-1 bg-transparent font-sans text-base text-ink outline-none placeholder:text-muted/60"
          />
        </div>

        {open ? (
          available.length > 0 ? (
            <ul className="absolute z-20 mt-1 max-h-52 w-full overflow-auto border border-border bg-surface">
              {available.map((o) => (
                <li key={o}>
                  <button
                    type="button"
                    onClick={() => add(o)}
                    className="block w-full px-3 py-2 text-left font-sans text-base text-ink transition-colors hover:bg-bg"
                  >
                    {o}
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <div className="absolute z-20 mt-1 w-full border border-border bg-surface px-3 py-2 font-sans text-sm text-muted">
              {query.trim() ? "No matching options" : "All options selected"}
            </div>
          )
        ) : null}
      </div>
    </Field>
  );
}
