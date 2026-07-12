import { CopyButton } from "../ui/CopyButton";

// One-time reveal of a newly-created repo's webhook secret. Rendered inside a
// non-dismissible Modal so the operator must explicitly acknowledge (the value
// is never retrievable again — subsequent GETs omit it). This is NOT the HEC
// token or the git credential; it is the GitHub push-webhook HMAC secret.
export function WebhookSecretReveal({
  url,
  secret,
}: {
  url: string;
  secret: string;
}) {
  return (
    <div className="space-y-4">
      <p className="text-sm text-slate-300">
        Repository <span className="font-medium text-slate-100">{url}</span> was
        registered. Copy its webhook secret now and add it to the GitHub push
        webhook. It is shown only once and cannot be retrieved later.
      </p>

      <div className="space-y-1">
        <span className="text-xs font-medium text-slate-400">Webhook secret</span>
        <div className="flex items-stretch gap-2">
          <code className="flex-1 select-all break-all rounded-md border border-surface-muted bg-surface px-3 py-2 font-mono text-xs text-emerald-300">
            {secret}
          </code>
          <CopyButton value={secret} />
        </div>
      </div>

      <div className="rounded-md border border-surface-muted bg-surface px-3 py-2 text-xs text-slate-400">
        <p className="font-medium text-slate-300">Configure the GitHub webhook</p>
        <ul className="mt-1 list-disc space-y-0.5 pl-4">
          <li>
            Payload URL: <code className="text-slate-300">/api/hooks/github</code>{" "}
            on this host
          </li>
          <li>Content type: application/json</li>
          <li>Secret: the value above</li>
          <li>Events: just the push event</li>
        </ul>
      </div>
    </div>
  );
}
