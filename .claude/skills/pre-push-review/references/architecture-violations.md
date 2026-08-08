# Architecture Violation Checks

**Single source of truth: `.claude/rules/architecture.md`.** The rules themselves
(dependency direction, per-layer responsibilities, forbidden operations) live there
and are NOT duplicated here — this file only assigns severity and gives the check
procedure. Read `architecture.md` first, then apply the severity mapping below.

**Dependency direction is machine-checked** by `tests/unit/test_architecture_deps.py`
(AST scan + `_BASELINE` ratchet). That test is authoritative for reverse imports; this
file's remaining job is the parts a static import scan cannot see — the human-reviewed
WARN criteria below. Do not re-judge dependency direction by eye.

## Severity Mapping

| Violation type (defined in `architecture.md`) | Severity | Checked by |
|---|---|---|
| Dependency direction — a layer imports something its "must not import" list forbids (§依存方向) | **FAIL** | `tests/unit/test_architecture_deps.py` (machine) |
| Layer constraint — Models mutability / `to_dict()` None / Manager DI / Service `config.py` access / Router business logic (§各層の責務と関心の分離) | **FAIL** | human review |
| Responsibility placement — Manager doing I/O (HTTP, file, dataframe, boto3, URL parsing) or Service doing business rules / cross-service workflow / user-facing message assembly | **WARN** | human review |

Do not restate the rules here. When `architecture.md` changes (a new layer, a new
exception like the Router display-helper carve-out, a `service_factory.py`-style
exemption), update the machine check's rules/allow-lists in `test_architecture_deps.py`
to match; this reference file already defers to the source and needs no edit.

### Responsibility placement — exemptions (WARN only)

- Lazy `import` inside a Manager solely to pass a Service method through
- Direct use inside test code

## How to Check

1. Dependency direction: `uv run pytest tests/unit/test_architecture_deps.py -q`. A failure
   names the file and the forbidden import → **FAIL**. Do not scan imports by hand.
2. For each changed Python file, identify its layer from the path (`src/models/`,
   `src/services/`, `src/managers/`, `src/managers/shared/`, `web/routers/`) and look up
   that layer's rules in `architecture.md`.
3. Verify the layer's constraints (§各層の責務と関心の分離) → violations = **FAIL**.
4. Check responsibility placement (Manager with I/O, Service with business rules) → **WARN**.
5. Report each with specific file, line, and the violated rule (cite the `architecture.md` section).
