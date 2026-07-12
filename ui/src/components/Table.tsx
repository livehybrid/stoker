import type { ReactNode } from "react";
import { cn } from "./cn";

// A minimal, generic table. Columns declare a header and a cell renderer; the
// page owns the row data and keying so it stays flexible for the page-builders.
export interface Column<Row> {
  key: string;
  header: ReactNode;
  cell: (row: Row) => ReactNode;
  className?: string;
}

interface TableProps<Row> {
  columns: Column<Row>[];
  rows: Row[];
  rowKey: (row: Row) => string | number;
  onRowClick?: (row: Row) => void;
  empty?: ReactNode;
}

export function Table<Row>({
  columns,
  rows,
  rowKey,
  onRowClick,
  empty,
}: TableProps<Row>) {
  if (rows.length === 0 && empty) {
    return <>{empty}</>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-surface-muted text-left text-xs uppercase tracking-wide text-slate-400">
            {columns.map((col) => (
              <th key={col.key} className={cn("px-3 py-2 font-medium", col.className)}>
                {col.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={rowKey(row)}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              className={cn(
                "border-b border-surface-muted/50",
                onRowClick && "cursor-pointer hover:bg-surface-muted/40",
              )}
            >
              {columns.map((col) => (
                <td key={col.key} className={cn("px-3 py-2 align-top", col.className)}>
                  {col.cell(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
