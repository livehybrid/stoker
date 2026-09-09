import type { ReactElement, ReactNode } from "react";
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
}

interface TableProps<Row> {
  columns: Column<Row>[];
  rows: Row[];
  rowKey: (row: Row) => string | number;
  onRowClick?: (row: Row) => void;
  empty?: ReactNode;
  /** Per-row actions menu. Must be a Splunk `Menu`. */
  rowActions?: (row: Row) => ReactElement | undefined;
}

export function Table<Row>({
  columns,
  rows,
  rowKey,
  onRowClick,
  empty,
  rowActions,
}: TableProps<Row>) {
  if (rows.length === 0 && empty) {
    return <>{empty}</>;
  }
  return (
    <SplunkTable horizontalOverflow="scroll">
      <SplunkTable.Head>
        {columns.map((col) => (
          <SplunkTable.HeadCell key={col.key} align={col.align} tooltip={col.tooltip}>
            {col.header}
          </SplunkTable.HeadCell>
        ))}
      </SplunkTable.Head>
      <SplunkTable.Body>
        {rows.map((row) => (
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
