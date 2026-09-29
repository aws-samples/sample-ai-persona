---
name: e2e
description: This skill should be used when the user asks to run E2E tests "in parallel", "with multiple agents", "並列で", "サブエージェントを立てて", or wants comprehensive E2E coverage split across concurrent agents. Fans out one `e2e-test` subagent per section group of docs/user_guide.md, each with an isolated playwright-cli session, then aggregates the results.
---

# Parallel E2E Test Runner

Split `docs/user_guide.md` into independent section groups and run one `e2e-test`
subagent per group concurrently, instead of one agent walking every scenario serially.

## Preconditions

1. **Resolve target environment before dispatching:**
   - If the user gave a URL (e.g. a staging CloudFront URL) and credentials, use those.
   - Otherwise default to local: `http://localhost:8000` (assume `run_htmx.py` is already running; do not start it yourself).
   - If the target needs login (Cognito Hosted UI etc.) and no credentials were given, ask the user for them before dispatching — do not guess or skip auth.

2. **Confirm current working directory is the project root**
   (`git rev-parse --show-toplevel`), not a subdirectory like `cdk/`. `playwright-cli`
   can only upload files that live under its launch directory — if any earlier command
   in this session `cd`'d elsewhere, dispatching from there silently breaks every
   upload-based scenario. Explicitly `cd` back to the root before launching agents.

## Splitting the work

3. Read `docs/user_guide.md` and enumerate its numbered `##` sections yourself — do not
   hardcode a section list, it drifts as the guide changes.

4. Group sections into 3–5 independent batches. Keep a data dependency in mind: discussion/interview
   scenarios (§3–4) need at least 2 saved personas to exist, so either:
   - put persona generation (§1–2) and discussion/interview (§3–4) in the **same** batch, or
   - tell the discussion/interview batch to check the persona list first and create its own
     minimal set (2–3 personas) if none exist yet, so it doesn't block on another batch's progress.

   A reasonable default split:
   - Batch A: §1 ペルソナ生成, §2 ペルソナ管理
   - Batch B: §3 議論・インタビューの実行, §4 議論結果の確認と保存 (self-sufficient — creates its own personas if needed)
   - Batch C: §5 議論・インタビュー履歴, §6 マスアンケート機能
   - Batch D: §7 外部データセット連携（実験的機能）
   - Batch E: §8 システム設定（データ連携機能・データセット管理・ナレッジベース管理・データ分析エージェント連携設定）

   Adjust batch count/boundaries if the guide's structure has changed.

   The `e2e-test` agent walks the happy path described in the guide by default. Two
   areas need an explicit nudge in the batch prompt, or they'll silently be skipped:
   - **CSV-sourced persona generation** (Batch A): §1 documents that `.csv` uploads take
     a different internal path (20-line preview + query-tool access to the rest) than
     other file types. Tell Batch A to run at least one generation with a CSV data
     source and check the 生成ログ・評価 for actual tool_call/tool_result evidence, not
     just assume the happy-path text-file scenario covers it.
   - **Upload limit / error-path testing**: the boundary values in 「アップロード上限一覧」
     (effective use tips section) aren't scenarios the agent will exercise on its own —
     they're reference data for humans. If the user wants limit/error-case coverage,
     assign it explicitly to the batch that owns the relevant feature (e.g. persona
     source size → Batch A, discussion attachments → Batch B, dataset upload → Batch E)
     and say so in that batch's prompt; don't expect it to happen implicitly.

## Dispatching (isolation is the whole point)

5. Launch one `Agent` call per batch, `subagent_type: e2e-test`, all in a **single message**
   (parallel tool calls) so they actually run concurrently — sequential calls in separate
   messages serialize them. Each prompt MUST include, verbatim adapted to that batch:

   - **Target env + credentials** (from step 1).
   - **Pin the working directory**: "必ずプロジェクトルート `<absolute-path>` で作業してください（cdで移動しない）".
   - **Full session + output isolation** so concurrent browsers don't collide. A unique
     `-s=<batch-name>` alone is **not enough**: `playwright-cli` shares one browser-server
     process and one output directory (`.playwright-cli/`) across the workspace, so a single
     command that omits the session drops onto the shared *default* session (→ mid-run
     `about:blank` / forced re-login, one batch's page bleeding into another), and every
     batch's snapshots/screenshots pile into the same directory where `ls -t` grabs a
     sibling's file. Instruct each agent to prefix **every** `playwright-cli` invocation
     (including `open`/`close`/`list`/`snapshot`/`screenshot`/`console`) with, verbatim
     (the harness resets env between Bash calls, so it must be inline on each call):
     ```bash
     PLAYWRIGHT_CLI_SESSION=<batch-name> \
     PLAYWRIGHT_MCP_OUTPUT_DIR="$PWD/tmp/e2e_<batch-name>/pw-out" \
     PLAYWRIGHT_MCP_USER_DATA_DIR="$PWD/tmp/e2e_<batch-name>/profile" \
     playwright-cli <command> ...
     ```
     Pick a distinct `<batch-name>` per batch (e.g. `persona`, `discussion`, `survey`,
     `dataset`, `settings`). Also tell the agent to (a) run `playwright-cli list` after the
     first `open` and confirm only its own session is present, and (b) reference the exact
     file path each `snapshot`/`screenshot` prints — **never** `ls -t` a shared dir.
   - **A unique scratch dir** under the repo root, `tmp/e2e_<batch-name>/`, holding both
     the per-batch playwright output (`pw-out/`) and browser profile (`profile/`) above,
     plus any crafted upload files. Create before use and delete when the batch finishes.
   - **Reuse `sample_data/` and `tests/test_file/` for happy-path upload files**
     (CSV sources, PDF, JPEG — see `e2e-test`'s own Test Data section for the mapping).
     Both are read-only and shared safely across concurrent batches, so no need to
     copy them into the batch's scratch dir. Only files crafted for boundary/error-path
     tests (oversized files, bad extensions, etc.) go in the scratch dir.
   - **The exact section numbers this batch owns**, and an explicit instruction not to
     touch sections owned by other batches (avoids duplicate/conflicting work and
     duplicate data creation).
   - **Data safety**: never delete data that isn't verifiably self-created this run;
     skip destructive tests (bulk delete, etc.) that would affect sibling batches'
     in-flight data.
   - Standard `e2e-test` reporting contract: Japanese report, pass/fail per section,
     discrepancies vs `user_guide.md`, evidence (screenshots/snapshots).

6. Run all batches with `run_in_background: true` — comprehensive E2E with real
   Bedrock calls (persona generation, discussion, report generation) takes minutes
   per batch; don't block the conversation waiting synchronously.

## Aggregating results

7. Wait for all batch completion notifications (don't poll or fabricate results
   before they land). When every batch has reported:
   - Merge into one structured report: per-section pass/fail, cross-batch discrepancy
     list, evidence references.
   - If any batch surfaced a `user_guide.md` vs UI discrepancy, surface it explicitly —
     the `e2e-test` agent's own protocol is to stop and ask before resolving those,
     so make sure the user actually sees each one instead of it being buried in a
     per-batch sub-report.
   - Confirm each batch cleaned up its `tmp/e2e_<batch-name>/` directory; clean up any
     that didn't.
