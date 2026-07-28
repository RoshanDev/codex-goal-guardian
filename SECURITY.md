# Security

Codex Goal Guardian uses the supported Codex App Server interface exposed by
the configured CLI. It does not read or copy Codex authentication files, edit
Codex session databases, patch desktop application packages, or store prompt
content.

Guardian logs contain health results, target names, outage generations, thread
IDs, action names, and compact error text. The optional Stop hook filters its
input to a small allowlist and discards prompts and tool payloads.

Keep the Guardian configuration, state, and logs user-readable only. They are
excluded from this repository. Review private-repository access before sharing
diagnostic logs because thread IDs and local paths can still be sensitive.

Report a vulnerability through a private GitHub security advisory for this
repository. Do not include credentials, session exports, or private prompts in
an issue.
