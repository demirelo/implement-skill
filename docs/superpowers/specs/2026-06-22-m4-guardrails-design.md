# Design — M4: guardrails (sandbox · worktree · destructive-gating · kill · suitability)

**Status:** design proposal, awaiting sign-off (2026-06-22)
**Parent design:** [`docs/design.md`](../../design.md) §8 · hardening backlog H6–H9
**Milestone:** **M4** — make `/implement` safe on a **real/untrusted** repo. The blocker is **H6** (the gate runs model-produced code unsandboxed). Full M4 in one pass: H6 sandbox, H7/H8 worktree isolation, H9 scored adapter detection, plus the §8 guardrails — destructive-command gating, kill criteria, stop-and-ask, suitability filter.

## 1. Locked decisions (from the crux questions)

- **Sandbox = Seatbelt default + Docker + hard-refuse.** macOS `sandbox-exec` (verified working here) is the per-candidate default; a Docker backend for portable/CI isolation; and a **hard refusal** to run an untrusted repo when no sandbox backend is available. Safe-by-default: a repo is **untrusted unless the operator marks it trusted**.
- **Full M4** — all of H6/H7/H8/H9 + every §8 guardrail in this milestone.

## 2. Module map

| File | Concern (testable seam) |
|---|---|
| `skills/implement/scripts/sandbox.py` (create) | **H6**: `wrap(argv, …)` for `seatbelt`/`docker`/`none`; `choose_backend(trusted, available)` policy; `available_backends`; `SandboxUnavailable`. |
| `skills/implement/scripts/guard.py` (create) | Destructive-command gating: `classify(argv) -> Verdict(safe, reason)` — deterministic deny patterns + allow-known-gate-cmds. |
| `skills/implement/scripts/workspace.py` (create) | **H7/H8**: `create_worktree`/`reset_worktree`(`-fdx`, scoped)/`remove_worktree` in **in-project `.worktrees/`**; `repo_context` (tracked files, heavy-dir ignore, char budget, decode-tolerant). |
| `skills/implement/scripts/kill.py` (create) | Kill criteria + stop-and-ask: `KillCriteria`, `should_stop(history) -> StopDecision(stop, blocker_type, reason)` — caps, gutter, 3-strike, denial. |
| `skills/implement/scripts/suitability.py` (create) | Suitability filter: `assess(adapter, acceptance_tests) -> Suitability(autonomous_ok, reasons)` — autonomous only if an objective oracle exists. |
| `skills/implement/scripts/gate.py` (modify) | **H9** scored `detect_adapter` (src/ layout, monorepo, uv/poetry/tox markers) + `install_cmd`; `run_gate` accepts an optional `wrap` callable. |
| `skills/implement/scripts/execute.py` (modify) | Integrate worktree + sandbox-wrap + guard + kill into `run_inner_loop`/`run_best_of_n`. |
| `skills/implement/scripts/implement.py` (modify) | `suitability.assess` gate at entry — refuse autonomous mode with no oracle. |
| `skills/implement/scripts/adapters/*.json` (modify) | `install_cmd` + richer detection markers. |
| `skills/implement/references/guardrails.md` (create) | Orchestration prose for the safety gates. |

Every new module keeps the codebase convention: injected `runner`, argv-building tested with a `FakeRun`, real-git modules tested against a fixture; the **actual isolation** is a manual live smoke.

## 3. `sandbox.py` — H6 (the blocker)

```python
class SandboxUnavailable(RuntimeError): ...   # untrusted repo + no backend -> hard refuse

def available_backends(runner=subprocess.run) -> list[str]
    # probes: "seatbelt" if `which sandbox-exec`, "docker" if `which docker`; "none" always last.
def choose_backend(*, trusted: bool, available: list[str], prefer="seatbelt") -> str
    # trusted  -> "none" allowed (defense-in-depth sandbox still used if available & prefer set).
    # untrusted-> first of (prefer, seatbelt, docker) in `available`; raise SandboxUnavailable if none.
def seatbelt_profile(workdir: str, tmpdir: str) -> str
    # (version 1)(deny default)(allow process*)(allow file-read*)(allow mach-lookup)(allow sysctl-read)
    # (allow file-write* (subpath workdir)(subpath tmpdir)(literal "/dev/null"))(deny network*)
def wrap(argv: list, *, backend: str, workdir: str, image="python:3.11", tmpdir="/tmp") -> list
    # seatbelt -> ["sandbox-exec","-p",seatbelt_profile(...),*argv]
    # docker   -> ["docker","run","--rm","--network=none","--memory=2g","--cpus=2",
    #             "-v",f"{workdir}:/work","-w","/work",image,*argv]
    # none     -> argv unchanged
```
**Invariant:** network denied + filesystem writes confined to the worktree (+ tmp) for every gate run on an untrusted repo; secrets dirs (`~/.ssh`, keychain, cloud creds) are not writable and — under Seatbelt's `deny network*` + write-confinement — cannot be exfiltrated by a malicious test. A live smoke proves: (a) a socket connect inside the sandbox fails, (b) a write to `$HOME/escape.txt` fails, (c) the legit fixture gate still passes green.

## 4. `guard.py` — destructive-command gating (deterministic)

```python
@dataclass(frozen=True)
class Verdict: safe: bool; reason: str = ""
def classify(argv) -> Verdict
    # DENY (substring/regex over the joined argv): rm -rf outside the worktree, ":(){...}" fork bomb,
    # curl|sh / wget|sh pipes, sudo, mkfs/dd/`> /dev/`, chmod -R 777, `git push --force`, writes to
    # ~/.ssh|~/.aws|keychain. ALLOW the known gate/install verbs (pytest, ruff, mypy, pip/uv/poetry
    # install, npm/pnpm, forge test). Default: deny unknown shelling-out, allow plain tool invocations.
```
Gates the **commands the harness itself runs** (adapter `test_cmd`/`lint_cmd`/`install_cmd`) before invocation — a malicious adapter or repo build-config can't run a destructive command. (Model-authored *code* is H6 sandbox's job; this is the command layer.)

## 5. `workspace.py` — H7/H8 (worktree isolation)

```python
def create_worktree(repo, wid, *, base="HEAD", runner=subprocess.run) -> str
    # git worktree add --detach <repo>/.worktrees/<wid> <base>  -> returns the worktree path.
    # tracked files only (no .venv/build copy); never mutates the live working tree.
def reset_worktree(path, runner=subprocess.run) -> None
    # git -C <path> reset --hard -q && git -C <path> clean -fdxq   (H7: -x cleans ignored, SCOPED to the worktree)
def remove_worktree(repo, path, runner=subprocess.run) -> None
    # git worktree remove --force <path>
def repo_context(path, *, max_chars=12000, ignore=(".git",".venv","node_modules","dist","build")) -> str
    # H8: read git-tracked *.py only, skip heavy/ignored dirs + is_secret_file, char-budget, decode-tolerant.
```
**Integration:** `execute.run_best_of_n` creates one worktree per candidate (in `.worktrees/`), runs the inner loop there, removes it after; `execute._reset` → `reset_worktree` (scoped, never the live tree — H7). `_repo_context` → `workspace.repo_context` (H8). `.worktrees/` is git-ignored.

## 6. `kill.py` — kill criteria + stop-and-ask

```python
BlockerType = "CAP_REACHED" | "GUTTER" | "THREE_STRIKE" | "DENIAL_CAP" | "NO_ORACLE"
@dataclass(frozen=True)
class KillCriteria: max_turns=6; max_no_progress=3; max_denials=4; strike_window=3
@dataclass(frozen=True)
class StopDecision: stop: bool; blocker_type: str = ""; reason: str = ""
def should_stop(history, crit=KillCriteria()) -> StopDecision
    # CAP_REACHED: turns >= max_turns. GUTTER: same failing-test set repeats max_no_progress times with
    # no new green. THREE_STRIKE: strike_window successive turns each fix one test but break another.
    # DENIAL_CAP: patch-apply/guard denials >= max_denials. Returns the first blocker that trips.
```
`history` is the per-turn ledger the inner loop already builds (failing sets, applied/denied, green delta). Named blockers feed **stop-and-ask**: the orchestrator halts and surfaces `blocker_type + reason` to the human rather than burning the cap silently.

## 7. `suitability.py` — suitability filter

```python
@dataclass(frozen=True)
class Suitability: autonomous_ok: bool; reasons: tuple
def assess(*, adapter: dict | None, acceptance_tests: list) -> Suitability
    # autonomous_ok iff a gate adapter was detected AND >=1 acceptance test exists (the RED-validated
    # M2 oracle). No oracle -> refuse autonomous mode (a green with no oracle is vacuous — H5 in spirit).
```
Called at the top of `run_implement` (after Phase 1 authored the oracle); if not `autonomous_ok`, the loop refuses to spend and stop-and-asks `NO_ORACLE`.

## 8. H9 — scored adapter detection

`gate.detect_adapter` becomes a **scored** match (not first-marker-wins): weight `pyproject.toml`/`setup.py`/`conftest.py` + `src/` layout + `uv.lock`/`poetry.lock`/`tox.ini` + monorepo (`packages/`,`apps/`) signals, pick the highest-scoring adapter, and surface its `install_cmd` (e.g. `uv sync` / `poetry install` / `pip install -e .`). Adapters gain `install_cmd` + a `markers` weight map. `guard.classify` runs on `install_cmd` before it executes.

## 9. Integration + testing

`run_inner_loop`/`run_best_of_n` thread: `workspace` worktrees, a `sandbox.wrap` callable into `run_gate`, `guard.classify` on adapter cmds (deny → `DENIAL_CAP`), and `kill.should_stop` each turn. `implement.run_implement` calls `suitability.assess` first and `sandbox.choose_backend` (refuse if untrusted + no backend). All helpers pure-unit/offline (`FakeRun`, real-git fixture). **Live smokes:** (1) Seatbelt — network-deny + fs-escape-deny + fixture-still-green; (2) worktree end-to-end on the fixture.

## 10. Build plan

Parallel (independent, disjoint files): `sandbox.py` · `guard.py` · `workspace.py` · `kill.py` · `suitability.py`. Then the integration pass (`gate.py` H9, `execute.py` wiring, `implement.py` suitability gate, adapter JSON), the two live smokes, an adversarial review, and tag `m4-guardrails`.

## 11. Out of scope (deferred)

A full container image build/registry (Docker backend uses a stock `python:3.11` + the worktree mount); Linux `bubblewrap`/`firejail` profiles (Seatbelt is macOS-only — Docker is the cross-OS path); per-syscall seccomp; network allow-listing (M4 denies network outright); the v2 native-agentic Builder hands. M5 (the learned router) consumes M4's per-run outcome ledger.
