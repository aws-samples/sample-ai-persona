# Security Review Criteria

Apply to all changed files in the diff.

## Secrets & Credentials

- No hardcoded API keys, tokens, passwords, or secrets
- No `.env` files in commit staging area
- No AWS access key IDs or secret access keys in source
- No boto3 clients with hardcoded credentials

## CDK/IAM Security

- No overly permissive IAM policies (`Effect: Allow, Action: *`)
- No `RemovalPolicy.DESTROY` on stateful resources
- No `grantAdmin()` or `iam.PolicyStatement` with `*` resource

## Application Security

- No SQL injection vectors (raw string interpolation in queries)
- No XSS vectors (unescaped user input in templates)
- No command injection (user input in shell commands)
- No SSRF vectors (user-controlled URLs in server-side requests)

## Information Disclosure

- No raw exception/stack trace leaking to user-facing error responses
  - `except Exception as e` blocks must not pass `str(e)` or `traceback` into HTTP response bodies
  - Return generic error messages ("Internal server error") to clients; log details server-side only
- No internal file paths, hostnames, or infrastructure details in error messages
- No debug mode enabled in production configurations (`DEBUG=True`, `app.debug`, `--reload`)
- No verbose error handlers that expose framework internals (e.g. FastAPI default 500 with traceback in non-debug)

## Dependency Security

- No pinned vulnerable package versions
- No packages from untrusted registries
