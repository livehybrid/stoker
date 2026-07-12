import { useState } from "react";
import { Button } from "../../components/Button";
import { copyToClipboard } from "../format";

// A button that copies `value` and flips to "Copied" briefly. Page-owned.
export function CopyButton({
  value,
  label = "Copy",
  className,
}: {
  value: string;
  label?: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <Button
      variant="secondary"
      className={className}
      onClick={async () => {
        const ok = await copyToClipboard(value);
        if (ok) {
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1500);
        }
      }}
    >
      {copied ? "Copied" : label}
    </Button>
  );
}
