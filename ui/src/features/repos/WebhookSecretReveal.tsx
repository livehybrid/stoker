import styled from "styled-components";
import { variables } from "@splunk/themes";
import { CopyButton } from "../ui/CopyButton";
import { Bullets, Inline, Label, Mono, Panel, Stack, Strong } from "../../components/text";

const Secret = styled.code`
  flex: 1;
  user-select: all;
  overflow-wrap: anywhere;
  border: 1px solid ${variables.borderColor};
  border-radius: ${variables.borderRadius};
  background-color: ${variables.backgroundColorSection};
  padding: ${variables.spacingSmall} ${variables.spacingMedium};
  font-family: ${variables.monoFontFamily};
  font-size: ${variables.fontSizeSmall};
  color: ${variables.successColor};
`;

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
    <Stack $gap="medium">
      <span>
        Repository <Strong>{url}</Strong> was
        registered. Copy its webhook secret now and add it to the GitHub push
        webhook. It is shown only once and cannot be retrieved later.
      </span>

      <Stack>
        <Label>Webhook secret</Label>
        <Inline>
          <Secret>{secret}</Secret>
          <CopyButton value={secret} />
        </Inline>
      </Stack>

      <Panel>
        <Strong>Configure the GitHub webhook</Strong>
        <Bullets>
          <li>
            Payload URL: <Mono>/api/hooks/github</Mono>{" "}
            on this host
          </li>
          <li>Content type: application/json</li>
          <li>Secret: the value above</li>
          <li>Events: just the push event</li>
        </Bullets>
      </Panel>
    </Stack>
  );
}
