import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../../lib/api";
import type { PackOut } from "../../lib/types";
import { Button } from "../../components/Button";
import { Field, TextInput } from "../../components/Field";
import { Bullets, Callout, Form, Grid, Inline, Strong } from "../../components/text";
import File from "@splunk/react-ui/File";

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
    <Form onSubmit={submit}>
      <Field
        label="Pack archive"
        hint="A .tar.gz, .tgz, .tar or .zip of the pack directory (wrapped in a folder or not — both work)."
      >
        {/* Splunk's File, which brings the drop target and the selected-file
            list rather than a bare <input type="file">. */}
        <File
          accept=".tar.gz,.tgz,.tar,.zip,application/gzip,application/zip,application/x-tar"
          allowMultiple={false}
          onRequestAdd={(added) => {
            setFile(added[0] ?? null);
            setLintFailed(null);
          }}
          onRequestRemove={() => {
            setFile(null);
            setLintFailed(null);
          }}
        >
          {file ? <File.Item key={file.name} name={file.name} /> : null}
        </File>
      </Field>

      <Grid $min="180px">
        <Field label="Name" hint="Optional; pack.yaml's name is used when blank.">
          <TextInput
            value={name}
            onChange={(_e, { value }) => setName(value)}
            placeholder="my-pack"
            spellCheck={false}
          />
        </Field>
        <Field label="Description" hint="Optional.">
          <TextInput
            value={description}
            onChange={(_e, { value }) => setDescription(value)}
            placeholder="What this pack generates"
          />
        </Field>
      </Grid>

      {(fieldError || apiMessage) && (
        <Callout $tone="error">
          {fieldError || apiMessage}
        </Callout>
      )}

      {lintFailed && (
        <Callout $tone="warning">
          <Strong>
            Uploaded as “{lintFailed.name}”, but it failed lint — fix the pack
            and upload again (runs are blocked until it lints clean):
          </Strong>
          <Bullets>
            {lintErrors.map((err) => (
              <li key={err}>{err}</li>
            ))}
          </Bullets>
        </Callout>
      )}

      <Inline>
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
      </Inline>
    </Form>
  );
}
