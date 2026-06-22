# Design — M3: GitHub draft PR + tiered handoff (Phase 3 · 4-post · 5)

**Status:** design proposal, awaiting sign-off (2026-06-22)
**Parent design:** [`docs/design.md`](../../design.md) §3 (PHASE 3 / 4 / 5)
**Milestone:** **M3** — put the reviewed winner on a GitHub PR. Phase 3 opens a **draft** PR; Phase 4's curated review is posted as a **summary comment**; Phase 5 flips to **ready-for-review** with a 🟢/🟡/🔴 tier + a structured body. The human merges — the loop never self-merges.

## 1. Locked decisions (from the crux questions)

- **Forge layer:** a **`gh`-only module** with a clean function seam (every op takes an injected `runner` + builds argv). Forge-neutrality is a later refactor (swap the module), not built now — YAGNI.
- **Review on the PR:** a **structured PR body + one curated summary comment** (findings rendered from `review.ReviewRound`). **No inline line-level comments** in v1 (deferred — avoids the diff-position mapping).
- **Verification:** unit-test every op offline against a fake runner, **plus a live smoke** — stand up a throwaway private repo, run the full open-draft → finalize flow against real GitHub, then delete it.

## 2. Module map

| File | Phase | Responsibility |
|---|---|---|
| `skills/implement/scripts/gh.py` (create) | 3·5 | Forge I/O via `gh`/`git`: branch+commit+push, open draft PR, post comment, mark ready, edit body. Injected `runner`; `ForgeError`. |
| `skills/implement/scripts/handoff.py` (create) | 5 | **Pure** rendering + tiering: `tier()` → green/yellow/red; `render_pr_body()`; `render_review_comment()`. |
| `skills/implement/scripts/publish.py` (create) | 3·5 | Thin orchestration composing `gh` + `handoff`: `open_draft()` (Phase 3), `finalize()` (Phase 5). The callable the live smoke + `run_implement` use. |
| `skills/implement/references/phase-3.md`, `phase-5.md` (create) | 3·5 | Orchestration prose. |
| `skills/implement/SKILL.md` (modify) | — | Phase 3/5 become real; link the new refs. |

Every op carries `runner=subprocess.run` and builds argv so tests capture `(argv, input)` with the existing `FakeRun`. PR/comment bodies are passed **via stdin** (`--body-file -`) — no argv length/escaping limits, and stdin is what `FakeRun` already records.

## 3. `gh.py` — forge I/O

```python
@dataclass(frozen=True)
class PrRef:
    number: int
    url: str
    branch: str

class ForgeError(RuntimeError): ...

def commit_and_push(repo, branch, message, *, base="main", sign=True, runner=subprocess.run) -> str
    # git checkout -b <branch>; git add -A; git commit (-c commit.gpgsign=false when sign=False);
    # git push -u origin <branch>. Returns the head SHA. (sign=True uses the repo's signing config —
    # the 1Password desktop integration; sign=False is the unattended path.)
def open_draft_pr(repo, *, branch, base, title, body, runner=subprocess.run) -> PrRef
    # gh pr create --draft --base <base> --head <branch> --title <title> --body-file -  (body on stdin)
    # parse the printed URL -> PrRef(number from trailing /pull/N, url, branch)
def post_comment(repo, pr, body, *, runner=subprocess.run) -> None      # gh pr comment <pr> --body-file -
def mark_ready(repo, pr, *, runner=subprocess.run) -> None               # gh pr ready <pr>
def update_body(repo, pr, body, *, runner=subprocess.run) -> None        # gh pr edit <pr> --body-file -
```
`pr` accepts a `PrRef` (uses `.url`), a number, or a URL — `gh` resolves all three. Any nonzero `gh`/`git` rc raises `ForgeError(stderr[:200])`, mirroring `execute.DispatchError`. `cwd=repo` on every call.

## 4. `handoff.py` — tiering + rendering (pure)

```python
TIER_EMOJI = {"green": "🟢", "yellow": "🟡", "red": "🔴"}

def tier(*, acceptance_green: bool, regate_passed: bool, review) -> str:
    # red   = not acceptance_green OR not regate_passed OR review.routed (an unresolved blocker/major)
    # yellow= (not red) AND review.escalated  (human must verify the can't-verify-from-diff findings)
    # green = otherwise (gates pass, nothing routed, nothing escalated; advisory-only is still green)
def render_review_comment(review) -> str        # markdown: ## Routed / ## Escalated (human-verify) / ## Advisory
def render_pr_body(*, goal, consensus_notes, acceptance_k, acceptance_n, review, tier_label) -> str
    # sections: ## Goal · ## Plan & consensus · ## Acceptance (k/N green) · ## Review summary ·
    #           ## Decisions needed / blocked / risks · a tier badge line
```
Pure string functions — no I/O, table-driven `tier` test, substring assertions on the rendered markdown. `review` is the `review.ReviewRound` from Phase 4 (`routed`/`escalated`/`advisory`/`decision`).

## 5. `publish.py` — orchestration (Phase 3 + Phase 5)

```python
@dataclass
class RunArtifacts:
    goal: str; branch: str; title: str; consensus_notes: str
    acceptance_k: int; acceptance_n: int; review: object; regate_passed: bool

def open_draft(repo, artifacts, *, base="main", sign=True, runner=subprocess.run) -> PrRef:
    # Phase 3: commit_and_push(...) then open_draft_pr(... body=a short "draft — review in progress" stub)
def finalize(repo, pr, artifacts, *, runner=subprocess.run) -> str:
    # Phase 5: compute tier(); update_body(full render_pr_body); post_comment(render_review_comment);
    # mark_ready(pr). Returns the tier label. (Phase 4's comment posting can also reuse post_comment.)
```
Sequencing only — every step delegates to a tested `gh`/`handoff` helper. Tested by asserting the `FakeRun` call **order** (commit→push→create, then edit→comment→ready).

## 6. Orchestration prose

- `phase-3.md`: after Phase 2's winner is materialized + re-gated, `publish.open_draft(repo, artifacts)` opens the draft PR; the branch name comes from the slice/goal. **Secrets boundary:** the PR body/comment are scrubbed (they may quote gate output/diffs) — reuse `scrub.scrub` before any forge call.
- `phase-5.md`: after Phase 4 resolves, `publish.finalize(repo, pr, artifacts)` sets the tier, writes the structured body, posts the curated review comment, and flips to ready-for-review. **Never** call a merge command — the human merges (touchpoint #2).
- `SKILL.md`: Phase 3 and Phase 5 lose their *(M3)* placeholders and link the two refs.

## 7. Testing

`test_gh.py` — argv + stdin-body for each op; URL→`PrRef` parse; `sign=False` injects `-c commit.gpgsign=false`; `ForgeError` on nonzero rc. `test_handoff.py` — `tier` truth table (green/yellow/red), body/comment contain goal·k/N·tier·grouped findings. `test_publish.py` — `open_draft`/`finalize` call **order** via `FakeRun`. All offline, existing conventions (`sys.path.insert`, `FakeRun` capturing `(argv, input)`, plain asserts).

**Live smoke (manual, end of build):** `gh repo create <user>/implement-m3-smoke --private`; seed a trivial repo; run `open_draft` then `finalize` against it; verify draft→ready, the body, and the comment; then `gh repo delete --yes` (confirmed before running). Authorized per the crux answer.

## 8. Build plan

`gh.py` and `handoff.py` are independent → build them **in parallel** (worktree-isolated implementers, TDD). Then `publish.py` (depends on both), the prose, the SKILL.md wiring, a final adversarial review, and the live smoke. Milestone tag `m3-github-pr`.

## 9. Out of scope (deferred)

Inline line-level review comments (diff-position mapping); a real forge **adapter** abstraction (GitLab/Gitea) — the `gh` module keeps a clean seam for that later; per-slice commits (v1 squashes the run into one commit); worktree isolation / destructive-command gating / kill-criteria (M4); the KB assembler/router (M5).
