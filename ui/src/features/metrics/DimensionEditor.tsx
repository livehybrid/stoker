import type { MetricDimension } from "../../lib/types";
import { Button } from "../../components/Button";
import { Field, TextInput } from "../../components/Field";

// Edit the dimension matrix: each dimension is a key plus a comma-separated list
// of values. The cross-product of all values is the series count (shown live).

interface Props {
  dimensions: MetricDimension[];
  onChange: (dims: MetricDimension[]) => void;
  seriesCount: number;
}

function parseValues(raw: string): string[] {
  return raw
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
}

export function DimensionEditor({ dimensions, onChange, seriesCount }: Props) {
  function update(index: number, patch: Partial<MetricDimension>) {
    onChange(dimensions.map((d, i) => (i === index ? { ...d, ...patch } : d)));
  }
  function remove(index: number) {
    onChange(dimensions.filter((_, i) => i !== index));
  }
  function add() {
    onChange([...dimensions, { key: "", values: [] }]);
  }

  return (
    <div className="space-y-3">
      {dimensions.length === 0 && (
        <p className="text-xs text-slate-500">
          No dimensions: a single unlabelled series. Add a dimension (e.g.
          product) to fan out into a matrix.
        </p>
      )}
      {dimensions.map((dim, i) => (
        <div key={i} className="flex items-end gap-2">
          <div className="w-40 shrink-0">
            <Field label={i === 0 ? "Dimension" : ""}>
              <TextInput
                placeholder="product"
                value={dim.key}
                onChange={(e) => update(i, { key: e.target.value })}
                autoComplete="off"
              />
            </Field>
          </div>
          <div className="flex-1">
            <Field
              label={i === 0 ? "Values (comma separated)" : ""}
              hint={i === dimensions.length - 1 ? `${dim.values.length} value(s)` : undefined}
            >
              <TextInput
                placeholder="checkout, search, catalog"
                defaultValue={dim.values.join(", ")}
                onBlur={(e) => update(i, { values: parseValues(e.target.value) })}
                autoComplete="off"
              />
            </Field>
          </div>
          <Button variant="ghost" onClick={() => remove(i)} className="mb-0.5">
            Remove
          </Button>
        </div>
      ))}
      <div className="flex items-center justify-between">
        <Button variant="secondary" onClick={add}>
          + Add dimension
        </Button>
        <span className="text-xs text-slate-400">
          → <span className="font-medium text-slate-200">{seriesCount}</span>{" "}
          series
        </span>
      </div>
    </div>
  );
}
