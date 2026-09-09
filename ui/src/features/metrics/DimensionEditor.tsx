import type { MetricDimension } from "../../lib/types";
import { Button } from "../../components/Button";
import { Field, TextInput } from "../../components/Field";
import { Between, Inline, Muted, Stack, Strong } from "../../components/text";

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
    <Stack>
      {dimensions.length === 0 && (
        <Muted $small>
          No dimensions: a single unlabelled series. Add a dimension (e.g.
          product) to fan out into a matrix.
        </Muted>
      )}
      {dimensions.map((dim, i) => (
        <Inline key={i}>
          <div>
            <Field label={i === 0 ? "Dimension" : ""}>
              <TextInput
                placeholder="product"
                value={dim.key}
                onChange={(_e, { value }) => update(i, { key: value })}
                autoComplete="off"
              />
            </Field>
          </div>
          <div>
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
          <Button variant="ghost" onClick={() => remove(i)}>
            Remove
          </Button>
        </Inline>
      ))}
      <Between>
        <Button variant="secondary" onClick={add}>
          + Add dimension
        </Button>
        <Muted $small>
          → <Strong>{seriesCount}</Strong>{" "}
          series
        </Muted>
      </Between>
    </Stack>
  );
}
