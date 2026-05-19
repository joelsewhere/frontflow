import { Field } from "./Field";

type GridValue = Record<string, string[]>;

interface CheckboxGridFieldProps {
  label: string;
  rows: readonly string[];
  columns: readonly string[];
  value: GridValue | undefined;
  onChange: (v: GridValue) => void;
  error?: string;
  hint?: string;
}

/**
 * A matrix of checkboxes — one row per `rows` entry, one column per
 * `columns` entry, every cell independently checkable. Controlled —
 * drive it with react-hook-form's <Controller>. Value maps each row to
 * the list of its checked columns: `{row: [columns]}`.
 */
export function CheckboxGridField({
  label,
  rows,
  columns,
  value,
  onChange,
  error, hint,
}: CheckboxGridFieldProps) {
  const v = value ?? {};
  const isChecked = (row: string, col: string) =>
    (v[row] ?? []).includes(col);

  const toggle = (row: string, col: string) => {
    const current = v[row] ?? [];
    const next = current.includes(col)
      ? current.filter((c) => c !== col)
      : [...current, col];
    onChange({ ...v, [row]: next });
  };

  return (
    <Field label={label} error={error} hint={hint}>
      <div
        className={`overflow-x-auto border ${
          error ? "border-error" : "border-border"
        }`}
      >
        <table className="w-full border-collapse">
          <thead>
            <tr>
              <th className="border-b border-border px-3 py-2" />
              {columns.map((col) => (
                <th
                  key={col}
                  className="border-b border-l border-border px-3 py-2 text-center font-mono text-xs font-medium uppercase tracking-wider text-muted"
                >
                  {col}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row}>
                <th className="border-t border-border px-3 py-2 text-left font-sans text-sm font-medium text-ink">
                  {row}
                </th>
                {columns.map((col) => (
                  <td
                    key={col}
                    className="border-l border-t border-border px-3 py-2 text-center"
                  >
                    <input
                      type="checkbox"
                      checked={isChecked(row, col)}
                      onChange={() => toggle(row, col)}
                      className="h-4 w-4 cursor-pointer accent-ink"
                      aria-label={`${row} — ${col}`}
                    />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Field>
  );
}
