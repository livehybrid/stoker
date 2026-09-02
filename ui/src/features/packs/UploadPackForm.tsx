import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../../lib/api";
import type { PackOut } from "../../lib/types";
import { Button } from "../../components/Button";
import { Field, TextInput } from "../../components/Field";

// The "Upload pack" form body (rendered inside a Modal by the Packs page).
// For the customer with no git access: a .tar.gz/.tgz/.tar or .zip of a pack
// directory, plus optional name/description overrides (pack.yaml fills them
// when omitted). The server detects the format from the content, extracts with
// its traversal/link/bomb guards, and registers the result as an ordinary
// local pack — INCLUDING when lint fails, in which case the 201 response
// carries the lint errors; we surface those here instead of closing, so the
// operator reads why their pack is bad without hunting for the pack card.
interface Props {
  onUploaded: (pack: PackOut) => void;
  onCancel: () => void;
}

export function UploadPackForm({ onUploaded, onCancel }: Props) {
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  // A registered-but-lint-failing pack: kept on screen with its errors.
  const [lintFailed, setLintFailed] = useState<PackOut | null>(null);

  const mutation = useMutation({
    mutationFn: (f: File) =>
      api.packs.upload(f, {
        name: name.trim() || undefined,
        description: description.trim() || undefined,
      }),
    onSuccess: (pack) => {
      // The pack row exists either way; refresh the grid, then either close
      // (clean lint) or stay open showing the errors.
      qc.invalidateQueries({ queryKey: ["packs"] });
      if (pack.lint_status === "ok") {
        onUploaded(pack);
      } else {
        setLintFailed(pack);
      }
    },
  });

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setFieldError(null);
    setLintFailed(null);
    if (!file) {
      setFieldError("Choose a pack archive to upload.");
      return;
    }
    mutation.mutate(file);
  }

  const apiMessage =
    mutation.error instanceof ApiError
      ? mutation.error.message
      : mutation.error instanceof Error
        ? mutation.error.message
        : null;

  const lintErrors = Array.isArray(lintFailed?.lint_errors_json)
    ? lintFailed.lint_errors_json.map(String)
    : [];

  return (
    <form onSubmit={submit} className="space-y-4">
      <Field
        label="Pack archive"
        hint="A .tar.gz, .tgz, .tar or .zip of the pack directory (wrapped in a folder or not — both work)."
      >
        <input
          type="file"
          accept=".tar.gz,.tgz,.tar,.zip,application/gzip,application/zip,application/x-tar"
          onChange={(e) => {
            setFile(e.target.files?.[0] ?? null);
            setLintFailed(null);
          }}
          className="block w-full text-sm text-slate-300 file:mr-3 file:rounded-md file:border-0 file:bg-surface-muted file:px-3 file:py-1.5 file:text-sm file:text-slate-100 hover:file:bg-surface"
        />
      </Field>

      <div className="grid grid-cols-2 gap-3">
        <Field label="Name" hint="Optional; pack.yaml's name is used when blank.">
          <TextInput
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="my-pack"
            spellCheck={false}
          />
        </Field>
        <Field label="Description" hint="Optional.">
          <TextInput
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What this pack generates"
          />
        </Field>
      </div>

      {(fieldError || apiMessage) && (
        <p className="rounded-md border border-red-800/60 bg-red-950/40 px-3 py-2 text-xs text-red-300">
          {fieldError || apiMessage}
        </p>
      )}

      {lintFailed && (
        <div className="rounded-md border border-amber-800/60 bg-amber-950/40 px-3 py-2 text-xs text-amber-200">
          <p className="font-medium">
            Uploaded as “{lintFailed.name}”, but it failed lint — fix the pack
            and upload again (runs are blocked until it lints clean):
          </p>
          <ul className="mt-1 list-disc space-y-0.5 pl-4 text-amber-300">
            {lintErrors.map((err) => (
              <li key={err}>{err}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex items-center justify-end gap-2 pt-1">
        <Button
          type="button"
          variant="ghost"
          onClick={onCancel}
          disabled={mutation.isPending}
        >
          {lintFailed ? "Close" : "Cancel"}
        </Button>
        <Button type="submit" variant="primary" disabled={mutation.isPending || !file}>
          {mutation.isPending ? "Uploading…" : "Upload pack"}
        </Button>
      </div>
    </form>
  );
}
