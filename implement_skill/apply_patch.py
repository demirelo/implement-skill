import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath


@dataclass
class ApplyResult:
    ok: bool
    error: str = ""


@dataclass(frozen=True)
class _Section:
    old: str
    new: str
    hunks: tuple[tuple[str, ...], ...]
    metadata: tuple[str, ...] = ()
    old_mode: str | None = None
    new_mode: str | None = None


_HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)
_MODE = re.compile(r"^(?:old mode|new mode|new file mode|deleted file mode) (\d+)$")


def _parse_sections(diff_text: str) -> tuple[_Section, ...]:
    """Parse all textual file sections, including every hunk, before applying anything."""
    lines = diff_text.splitlines()
    sections: list[_Section] = []
    pending_metadata: list[str] = []
    i = 0
    while i < len(lines):
        # A patch may contain a stat line beginning with `---`; only a ---/+++ pair is a
        # file section. Keep metadata from the current diff header for symlink validation.
        while i < len(lines):
            if lines[i].startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
                break
            if lines[i].startswith("diff --git "):
                pending_metadata = [lines[i]]
            elif lines[i].startswith(("old mode ", "new mode ", "new file mode ", "deleted file mode ",
                                      "rename from ", "rename to ")):
                pending_metadata.append(lines[i])
            i += 1
        if i >= len(lines):
            break

        old = lines[i][4:].strip()
        i += 1
        if i >= len(lines) or not lines[i].startswith("+++ "):
            raise ValueError("malformed patch: missing +++ path")
        new = lines[i][4:].strip()
        i += 1
        hunks: list[tuple[str, ...]] = []
        while i < len(lines):
            if lines[i].startswith("diff --git "):
                break
            if lines[i].startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
                break
            if not lines[i].startswith("@@ "):
                i += 1
                continue
            match = _HUNK_HEADER.match(lines[i])
            if match is None:
                raise ValueError(f"malformed patch hunk header: {lines[i]}")
            i += 1
            hunk: list[str] = []
            # `git apply --recount` intentionally treats the header counts as advisory. Consume
            # all hunk lines until the next structural marker so valid minimal-context patches
            # with stale counts retain their existing fallback behavior.
            while i < len(lines):
                line = lines[i]
                if line.startswith("diff --git ") or line.startswith("@@ "):
                    break
                if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
                    break
                if line.startswith("\\"):
                    # `\\ No newline at end of file` annotates the preceding hunk line.
                    i += 1
                    continue
                if not line or line[0] not in " +-":
                    break
                hunk.append(line)
                i += 1
            hunks.append(tuple(hunk))

        old_mode = new_mode = None
        for metadata in pending_metadata:
            mode_match = _MODE.match(metadata)
            if mode_match:
                if metadata.startswith("old mode ") or metadata.startswith("deleted file mode "):
                    old_mode = mode_match.group(1)
                else:
                    new_mode = mode_match.group(1)
        sections.append(_Section(old, new, tuple(hunks), tuple(pending_metadata), old_mode, new_mode))
        pending_metadata = []
    return tuple(sections)


def _sections(diff_text: str):
    """Compatibility view used by older callers of the structured fallback."""
    for section in _parse_sections(diff_text):
        yield section.old, section.new, [list(hunk) for hunk in section.hunks]


def _decode_patch_path(path: str) -> str:
    path = path.strip()
    if "\t" in path:
        path = path.split("\t", 1)[0]
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
        # Git's C-style quoting can encode traversal characters. Refuse escaped paths rather
        # than attempting a partial decode that could disagree with git's path interpretation.
        if "\\" in path:
            raise ValueError(f"unsupported escaped patch path: {path!r}")
    return path


def _strip_path(path: str) -> str:
    path = _decode_patch_path(path)
    if path == "/dev/null":
        return path
    # Match git apply -p1: strip exactly the first slash-delimited component, not only
    # conventional a/ and b/ prefixes. This makes X//tmp/evil become /tmp/evil before
    # containment validation rather than allowing git to reinterpret it later.
    _, separator, remainder = path.partition("/")
    return remainder if separator else path


def _strip_rename_path(path: str) -> str:
    # `rename from/to` metadata is already repository-relative and is not subject to -p1.
    return _decode_patch_path(path)


def _diff_header_paths(header: str) -> tuple[str, str]:
    """Tokenize a Git diff header without decoding its C-style quoted paths."""
    tokens: list[str] = []
    i = 0
    while i < len(header):
        while i < len(header) and header[i].isspace():
            i += 1
        if i >= len(header):
            break
        start = i
        if header[i] == '"':
            i += 1
            closed = False
            while i < len(header):
                if header[i] == "\\":
                    if i + 1 >= len(header):
                        raise ValueError(f"malformed diff header: {header}")
                    i += 2
                elif header[i] == '"':
                    i += 1
                    closed = True
                    break
                else:
                    i += 1
            if not closed:
                raise ValueError(f"malformed diff header: {header}")
            if i < len(header) and not header[i].isspace():
                raise ValueError(f"malformed diff header: {header}")
        else:
            while i < len(header) and not header[i].isspace():
                i += 1
        tokens.append(header[start:i])
    if len(tokens) != 2:
        raise ValueError(f"malformed diff header: {header}")
    return tokens[0], tokens[1]


def _find_block(lines: list[str], block: list[str]) -> int:
    if not block:
        return 0
    for start in range(0, len(lines) - len(block) + 1):
        if lines[start:start + len(block)] == block:
            return start
    return -1


def _relative_path(target: str, raw: str) -> str | None:
    if target == "/dev/null":
        return None
    posix = PurePosixPath(target)
    windows = PureWindowsPath(target)
    if (not target or posix.is_absolute() or windows.is_absolute() or windows.drive or
            ".." in posix.parts or ".." in windows.parts):
        raise ValueError(f"unsafe patch path (must stay repository-relative): {raw!r}")
    return target


def _relative_patch_path(raw: str) -> str | None:
    decoded = _decode_patch_path(raw)
    if decoded == "/dev/null":
        return None
    raw_posix = PurePosixPath(decoded)
    raw_windows = PureWindowsPath(decoded)
    if (raw_posix.is_absolute() or raw_windows.is_absolute() or raw_windows.drive or
            ".." in raw_posix.parts or ".." in raw_windows.parts):
        raise ValueError(f"unsafe patch path (must stay repository-relative): {raw!r}")
    return _relative_path(_strip_path(decoded), raw)


def _relative_rename_path(raw: str) -> str | None:
    return _relative_path(_strip_rename_path(raw), raw)


def _inside(root: Path, resolved: Path, description: str) -> Path:
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{description} escapes candidate root: {resolved}") from exc
    return resolved


class _VirtualSymlinks:
    """Resolve patch-created and retargeted links without touching the candidate tree."""

    def __init__(self, root: Path, targets: dict[Path, str]):
        self.root = root
        self.targets = targets
        self._memo: dict[Path, Path] = {}

    def _from_base(self, base: Path, target: str, stack: tuple[Path, ...]) -> Path:
        current = _inside(self.root, base.resolve(strict=False), "symlink path")
        for part in PurePosixPath(target).parts:
            if part in ("", "."):
                continue
            if part == "..":
                current = _inside(self.root, current.parent, f"symlink target {target!r}")
                continue
            candidate = current / part
            if candidate in self.targets:
                current = self.link(candidate, stack)
            else:
                current = _inside(self.root, candidate.resolve(strict=False), f"symlink target {target!r}")
        return current

    def link(self, path: Path, stack: tuple[Path, ...] = ()) -> Path:
        if path in stack:
            raise ValueError(f"symlink cycle detected at {path}")
        if path in self._memo:
            return self._memo[path]
        target = self.targets[path]
        parent_relative = path.parent.relative_to(self.root)
        parent_name = "." if not parent_relative.parts else parent_relative.as_posix()
        parent = self._from_base(self.root, parent_name, stack + (path,))
        resolved = self._from_base(parent, target, stack + (path,))
        self._memo[path] = resolved
        return resolved

    def candidate(self, relative: str) -> Path:
        # Check the actual tree first, so replacing an existing escaping symlink cannot hide it.
        _inside(self.root, (self.root / relative).resolve(strict=False), f"patch path {relative!r}")
        return self._from_base(self.root, relative, ())


def _symlink_target(section: _Section, existing_link: bool) -> str | None:
    if section.new_mode != "120000" and not existing_link:
        return None
    additions = [line[1:] for hunk in section.hunks for line in hunk if line.startswith("+")]
    if len(additions) != 1:
        raise ValueError("malformed symlink patch: expected one link target")
    return additions[0]


def _metadata_paths(diff_text: str):
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            try:
                paths = _diff_header_paths(line[len("diff --git "):])
            except ValueError as exc:
                raise ValueError(f"malformed diff header: {line}") from exc
            yield "header", paths[0]
            yield "header", paths[1]
        elif line.startswith(("rename from ", "rename to ")):
            yield "rename", line.split(" ", 2)[2]


def _validate_sections(repo_path, sections: tuple[_Section, ...], diff_text: str = "") -> None:
    root = Path(repo_path).resolve(strict=False)
    if not root.is_dir():
        raise ValueError(f"candidate root is not a directory: {repo_path}")

    # Collect every section and metadata path before checking dependent paths. Metadata-only
    # rename/binary patches still need containment validation even without ---/+++ sections.
    paths: list[str] = []
    link_targets: dict[Path, str] = {}
    for section in sections:
        old = _relative_patch_path(section.old)
        new = _relative_patch_path(section.new)
        if old is not None:
            paths.append(old)
        if new is not None:
            paths.append(new)

        existing_link = ((new is not None and (root / new).is_symlink()) or
                         (old is not None and (root / old).is_symlink()))
        target = _symlink_target(section, existing_link)
        if target is None or new is None:
            continue
        target_posix = PurePosixPath(target)
        target_windows = PureWindowsPath(target)
        if target_posix.is_absolute() or target_windows.is_absolute() or target_windows.drive:
            raise ValueError(f"unsafe symlink target for {new!r}: {target!r}")
        link_targets[root / new] = target

    for kind, raw in _metadata_paths(diff_text):
        relative = (_relative_patch_path(raw) if kind == "header" else _relative_rename_path(raw))
        if relative is not None:
            paths.append(relative)

    virtual_symlinks = _VirtualSymlinks(root, link_targets)
    # Resolve all virtual targets first, including links whose target names another virtual link.
    # Recursive cycles fail closed instead of relying on Path.resolve's view of the old tree.
    for link in link_targets:
        virtual_symlinks.link(link)
    for relative in paths:
        virtual_symlinks.candidate(relative)


def _apply_structured(repo_path, diff_text) -> ApplyResult:
    try:
        sections = _parse_sections(diff_text)
        _validate_sections(repo_path, sections, diff_text)
        if not sections:
            return ApplyResult(False, "no valid patches in input")

        root = Path(repo_path).resolve(strict=False)
        updates: dict[Path, list[str]] = {}
        for section in sections:
            target = _relative_patch_path(section.new if section.new != "/dev/null" else section.old)
            if not target:
                return ApplyResult(False, "structured fallback does not support delete-only diffs")
            path = root / target
            if section.new_mode == "120000":
                return ApplyResult(False, "structured fallback does not support symlink creation")
            if path.is_symlink():
                return ApplyResult(False, "structured fallback does not support writes through symlinks")
            if path not in updates:
                updates[path] = path.read_text().splitlines()
            lines = updates[path]
            for hunk in section.hunks:
                old_block = [line[1:] for line in hunk if line.startswith((" ", "-"))]
                new_block = [line[1:] for line in hunk if line.startswith((" ", "+"))]
                start = _find_block(lines, old_block)
                if start == -1:
                    return ApplyResult(False, f"structured fallback could not match hunk in {target}")
                lines[start:start + len(old_block)] = new_block

        # No filesystem write occurs until every section and hunk has parsed, validated, read,
        # and matched. This keeps an invalid later file from contaminating earlier files.
        for path, lines in updates.items():
            path.write_text("\n".join(lines) + ("\n" if lines else ""))
    except (OSError, ValueError) as exc:
        return ApplyResult(False, str(exc))
    return ApplyResult(ok=bool(updates), error="" if updates else "no valid patches in input")


def apply_patch(repo_path, diff_text) -> ApplyResult:
    repo = str(repo_path)
    if diff_text and not diff_text.endswith("\n"):
        diff_text += "\n"
    try:
        # This preflight must happen before git apply --check: git itself would read the candidate
        # tree before we have rejected hostile paths or links.
        _validate_sections(repo_path, _parse_sections(diff_text), diff_text)
    except (OSError, ValueError) as exc:
        return ApplyResult(False, str(exc))

    errors = []
    base = ["git", "apply", "--recount", "-p1", "-"]
    check = subprocess.run(base[:2] + ["--check"] + base[2:],
                           cwd=repo, input=diff_text, capture_output=True, text=True)
    if check.returncode == 0:
        proc = subprocess.run(base[:2] + ["--whitespace=nowarn"] + base[2:],
                              cwd=repo, input=diff_text, capture_output=True, text=True)
        if proc.returncode == 0:
            return ApplyResult(ok=True)
        errors.append(proc.stderr.strip())
    else:
        errors.append(check.stderr.strip() or "patch does not apply")
    fallback = _apply_structured(repo_path, diff_text)
    if fallback.ok:
        return fallback
    return ApplyResult(ok=False, error=fallback.error or (errors[-1] if errors else "patch does not apply"))
