# Security Policy

## Reporting a Vulnerability

If you believe you've found a security vulnerability in Bill Commons, please
report it responsibly by emailing **contact@billcommons.org**.

Please include:

* A description of the vulnerability and its potential impact.
* Steps to reproduce (proof-of-concept code or requests, if applicable).
* The affected component (e.g., `apps/api`, `apps/mcp`, `workers/ingest`,
  `apps/web`) and, if known, the affected version/commit.

Please do **not** open a public GitHub issue for security vulnerabilities.

## What to expect

* We will acknowledge receipt of your report within a reasonable timeframe.
* We will investigate and keep you informed of progress toward a fix.
* We ask that you give us a reasonable opportunity to address the issue
  before any public disclosure.

## Scope

Bill Commons is public infrastructure that republishes public-domain
legislative data. In-scope concerns include, but are not limited to:

* Authentication/authorization issues in the API or MCP server.
* Injection vulnerabilities (SQL, command, etc.).
* Denial-of-service vectors beyond normal rate-limit exhaustion.
* Exposure of credentials, connection strings, or internal infrastructure
  details.
* Data integrity issues that could allow tampering with ingested legislative
  records.

Thank you for helping keep Bill Commons and its users safe.
