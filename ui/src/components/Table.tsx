import { useMemo, useState, type ReactElement, type ReactNode } from "react";
import SplunkTable from "@splunk/react-ui/Table";

/*
 * A generic table, on Splunk's Table.
 *
 * The column-descriptor API is kept because every page in this app is built
 * from it: columns declare a header and a cell renderer, and the page owns the
 * row data and keying. What changed underneath is that the markup, the header
 * treatment, the hover and click affordances, the horizontal overflow and the
 * row actions are now Splunk's rather than a hand-rolled approximation.
 */
export interface Column<Row> {
  key: string;
  header: ReactNode;
  cell: (row: Row) => ReactNode;
  /** Right-align a numeric column, as Splunk's own tables do. */
  align?: "left" | "center" | "right";
  /** A tooltip on the header, for a column whose name needs explaining. */
  tooltip?: ReactNode;
  /**
   * Makes the column sortable: the value a row sorts by. Blank values
   * (null, undefined, "") always sort last, whichever way round.
   */
  sortValue?: (row: Row) => string | number | null | undefined;
}

export type SortDir = "asc" | "desc";
export interface SortState {
  key: string;
  dir: SortDir;
}

interface TableProps<Row> {
  columns: Column<Row>[];
  rows: Row[];
  rowKey: (row: Row) => string | number;
  onRowClick?: (row: Row) => void;
  empty?: ReactNode;
  /** Per-row actions menu. Must be a Splunk `Menu`. */
  rowActions?: (row: Row) => ReactElement | undefined;
  /** The sort before any header is clicked; the rows' own order if unset. */
  defaultSort?: SortState;
}

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

function blank(v: unknown): boolean {
  return v == null || v === "" || (typeof v === "number" && Number.isNaN(v));
}

export function sortRows<Row>(
  rows: Row[],
  value: (row: Row) => string | number | null | undefined,
  dir: SortDir,
): Row[] {
  const sign = dir === "asc" ? 1 : -1;
  // Indexed so equal values keep their incoming order.
  return rows
    .map((row, i) => ({ row, i, v: value(row) }))
    .sort((a, b) => {
      const ab = blank(a.v);
      const bb = blank(b.v);
      if (ab || bb) return ab === bb ? a.i - b.i : ab ? 1 : -1;
      const c =
        typeof a.v === "number" && typeof b.v === "number"
          ? a.v - b.v
          : collator.compare(String(a.v), String(b.v));
      return c !== 0 ? sign * c : a.i - b.i;
    })
    .map((x) => x.row);
}

export function Table<Row>({
  columns,
  rows,
  rowKey,
  onRowClick,
  empty,
  rowActions,
  defaultSort,
}: TableProps<Row>) {
  const [sort, setSort] = useState<SortState | null>(defaultSort ?? null);
  const sortCol = sort ? columns.find((c) => c.key === sort.key && c.sortValue) : undefined;
  const shown = useMemo(
    () => (sort && sortCol?.sortValue ? sortRows(rows, sortCol.sortValue, sort.dir) : rows),
    [rows, sort, sortCol],
  );
  if (rows.length === 0 && empty) {
    return <>{empty}</>;
  }
  return (
    <SplunkTable horizontalOverflow="scroll">
      <SplunkTable.Head>
        {columns.map((col) => (
          <SplunkTable.HeadCell
            key={col.key}
            align={col.align}
            tooltip={col.tooltip}
            {...(col.sortValue
              ? {
                  sortKey: col.key,
                  sortDir: sort?.key === col.key ? sort.dir : "none",
                  // The first click on a column sorts it descending (newest,
                  // largest first); the next click flips it.
                  onSort: () =>
                    setSort((cur) => ({
                      key: col.key,
                      dir: cur?.key === col.key && cur.dir === "desc" ? "asc" : "desc",
                    })),
                }
              : {})}
          >
            {col.header}
          </SplunkTable.HeadCell>
        ))}
      </SplunkTable.Head>
      <SplunkTable.Body>
        {shown.map((row) => (
          <SplunkTable.Row
            key={rowKey(row)}
            onClick={onRowClick ? () => onRowClick(row) : undefined}
            actionsSecondary={rowActions ? rowActions(row) : undefined}
          >
            {columns.map((col) => (
              <SplunkTable.Cell key={col.key} align={col.align}>
                {col.cell(row)}
              </SplunkTable.Cell>
            ))}
          </SplunkTable.Row>
        ))}
      </SplunkTable.Body>
    </SplunkTable>
  );
}
