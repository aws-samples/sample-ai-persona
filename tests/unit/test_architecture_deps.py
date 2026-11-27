"""Mechanically checks layer dependency direction (`.claude/rules/architecture.md` is the single source).

The pre-push-review "architecture violation check" was originally a one-off
LLM judgment applied to the diff, with no reproducibility or guarantee that it
ran. Dependency direction is a deterministic rule that only needs an import
scan, so it follows the approach established in error-catalog.md: discipline
is guaranteed by AST scanning, not manual review
(`tests/api/test_error_exposure.py`).

Dependency directions checked (architecture.md, dependency direction):
- Router -> Manager -> Service. Models may be referenced from every layer. No reverse imports.
- Models   : must not import services / managers / routers
- Service  : must not import managers / routers, nor other services (`service_factory` is the exception)
- Manager  : must not import routers, nor other managers (`shared/` and `components/` are allowed)
- Component: `src/managers/components/`, shared workflow parts used by several Managers.
             Manager -> Component is allowed; Component -> Manager / Router is not.
             Components may depend on each other one-way; cycles are not allowed
             (checked separately on a per-file graph)
- Routers must not import `AccessTarget` (the material of an access decision)
- Shared   : must not import services / routers
- Router   : must not import services directly
             (exception: template display helpers, see `_DISPLAY_HELPER_ALLOW`)

The check is a ratchet that must match `_BASELINE` exactly. The current code
has zero violations, so the baseline is empty and a single reverse import
fails. Responsibility placement checks (I/O in a Manager, business rules in a
Service, etc.) are subjective and do not suit static scanning, so they are out
of scope here (pre-push-review covers them as a WARN for manual review).
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SRC = Path(__file__).parent.parent.parent / "src"
_WEB = Path(__file__).parent.parent.parent / "web"

# Directory of each layer, and the layers it must not import (forbidden targets).
# Forbidden targets are leading module-path segments (judged after resolving relative and absolute imports).
_MODELS_DIR = _SRC / "models"
_SERVICES_DIR = _SRC / "services"
_MANAGERS_DIR = _SRC / "managers"
_SHARED_DIR = _SRC / "managers" / "shared"
_ROUTERS_DIR = _WEB / "routers"

# The only case where a Service may import another Service (architecture.md).
_SERVICE_FACTORY = "service_factory"

# Services a Router may import directly as template display helpers (architecture.md).
# Only pure data lookups (ISO country code -> name, etc.) with no business logic.
_DISPLAY_HELPER_ALLOW = {"country_service"}

# Subpackages treated as one unit per file. stores/ holds the per-table stores;
# stores must not import each other even though they share a directory.
_PER_FILE_PACKAGES = {"stores"}

# Known violations (module relative path -> set of violating import targets). Do not add
# new reverse imports here; move them to the correct layer. As a ratchet, new violations
# not listed here fail, and entries that remain after being fixed also fail.
#
# agent_service lazily imports sibling services at run time (AgentService directly
# bundles DataAgent / MCP / KnowledgeBase / Memory to build tools).
# This is existing debt that a Manager should orchestrate; delete the entry once fixed.
_BASELINE: dict[str, set[str]] = {
    "src/services/agent_service.py": {
        "src.services.data_agent_service",
        "src.services.knowledge_base.kb_tools",
        "src.services.memory.session_manager_factory",
    },
}


def _py_files(directory: Path, *, recursive: bool = False) -> list[Path]:
    pattern = "**/*.py" if recursive else "*.py"
    return sorted(p for p in directory.glob(pattern) if p.name != "__init__.py")


def _layer_modules() -> list[tuple[Path, Path]]:
    """List of (file path, owning layer directory). shared is handled separately from managers."""
    result: list[tuple[Path, Path]] = []
    result += [(p, _MODELS_DIR) for p in _py_files(_MODELS_DIR, recursive=True)]
    result += [(p, _SERVICES_DIR) for p in _py_files(_SERVICES_DIR, recursive=True)]
    result += [(p, _ROUTERS_DIR) for p in _py_files(_ROUTERS_DIR, recursive=True)]
    # Separate managers top level (excluding shared) from shared.
    shared_files = set(_py_files(_SHARED_DIR, recursive=True))
    for p in _py_files(_MANAGERS_DIR, recursive=True):
        if p in shared_files:
            result.append((p, _SHARED_DIR))
        else:
            result.append((p, _MANAGERS_DIR))
    return result


def _resolve_import(node: ast.AST, module_file: Path) -> str | None:
    """Normalize an import statement to dotted form such as `src.services.foo` / `web.routers.bar`.

    Relative imports (`from ..services.x`) are resolved from the module_file location.
    Returns None if unresolvable or outside the packages.
    """
    if isinstance(node, ast.Import):
        # `import src.services.x` form. Aliases are handled separately by the caller.
        return None
    if not isinstance(node, ast.ImportFrom):
        return None

    if node.level == 0:
        return node.module

    # Relative import. Resolve from the package (src / web) that module_file belongs to.
    # The parent of src/managers/foo.py is src/managers; `..services` at level=2 is src/services.
    parts = module_file.parent.parts
    # Use only the part after "src" or "web" under the project root.
    for anchor in ("src", "web"):
        if anchor in parts:
            idx = parts.index(anchor)
            base = list(parts[idx:])
            break
    else:
        return None

    # level=1 is the current directory, level=2 one up. Drop level-1 trailing parts.
    trim = node.level - 1
    if trim:
        base = base[:-trim] if trim <= len(base) else []
    tail = node.module.split(".") if node.module else []
    return ".".join(base + tail)


_LAYER_PKGS = {
    ("src", "models"),
    ("src", "services"),
    ("src", "managers"),
    ("web", "routers"),
}


def _iter_imported_dotted(tree: ast.AST, module_file: Path) -> list[str]:
    """List dotted import targets, resolving both relative and absolute imports.

    When the module itself is a layer package, as in `from src.services import country_service`,
    each imported name is treated as a submodule and expanded
    (`src.services.country_service`). When the module is a concrete module, the
    imported names are classes or functions and are not expanded.
    """
    type_checking_imports = _type_checking_import_nodes(tree)
    out: list[str] = []
    for node in ast.walk(tree):
        # Imports under `if TYPE_CHECKING:` are for type annotations only and create no runtime coupling.
        # Dependency direction is judged by run-time loading, so they are excluded.
        if node in type_checking_imports:
            continue
        if isinstance(node, ast.Import):
            out += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_import(node, module_file)
            if not resolved:
                continue
            seg = _segments(resolved)
            if len(seg) == 2 and (seg[0], seg[1]) in _LAYER_PKGS:
                out += [f"{resolved}.{alias.name}" for alias in node.names]
            else:
                out.append(resolved)
    return out


def _type_checking_import_nodes(tree: ast.AST) -> set[ast.AST]:
    """Collect import statement nodes directly under `if TYPE_CHECKING:` blocks."""
    nodes: set[ast.AST] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        is_tc = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )
        if is_tc:
            for stmt in node.body:
                if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                    nodes.add(stmt)
    return nodes


def _segments(dotted: str) -> list[str]:
    """`src.services.ai_service` -> ['src','services','ai_service'] (normalized to start at src/web)."""
    parts = dotted.split(".")
    for anchor in ("src", "web"):
        if anchor in parts:
            return parts[parts.index(anchor) :]
    return parts


def _unit_of(module_file: Path, layer_dir: Path) -> str:
    """Unit name within a layer. A subpackage (`memory/` etc.) yields its directory name,
    a top-level module yields its file name (no extension). Imports within one unit are not coupling.

    Resolved by path segment names so it works for real files and temp files
    (`relative_to` raises under the tests' tmp_path, so it is not used)."""
    key = layer_dir.name  # models / services / managers / shared / routers
    parts = module_file.parts
    if key in parts:
        after = parts[parts.index(key) + 1 :]
        if len(after) > 1:  # under a subpackage (after = [subpkg, ..., file.py])
            if after[0] in _PER_FILE_PACKAGES:
                return f"{after[0]}.{Path(after[1]).stem}"
            return after[0]
    return module_file.stem


def _target_unit(seg: list[str], layer_index: int) -> str:
    """Extract the in-layer unit name from a dotted import target.

    seg=['src','services','memory','memory_service'], layer_index=2 -> 'memory'
    seg=['src','services','country_service'],          layer_index=2 -> 'country_service'
    seg=['src','services','stores','persona_store'],   layer_index=2 -> 'stores.persona_store'
    """
    after = seg[layer_index:]
    if len(after) > 1 and after[0] in _PER_FILE_PACKAGES:
        return f"{after[0]}.{after[1]}"
    return after[0] if after else ""


def _violations_for(module_file: Path, layer_dir: Path) -> set[str]:
    """Collect reverse-import violations in one file. Returns the violating import targets (dotted).

    Within a layer, judgment is per unit: a top-level module name or a
    subpackage name (`memory/` etc.). Imports within one unit are not coupling, so they are allowed.
    """
    tree = ast.parse(module_file.read_text(encoding="utf-8"))
    self_unit = (
        _unit_of(module_file, layer_dir)
        if layer_dir != _SHARED_DIR
        else module_file.stem
    )
    is_factory = module_file.stem == _SERVICE_FACTORY
    found: set[str] = set()

    for dotted in _iter_imported_dotted(tree, module_file):
        seg = _segments(dotted)
        if len(seg) < 2:
            continue
        top, sub = seg[0], seg[1]
        target_pkg = (top, sub)

        if layer_dir == _MODELS_DIR:
            if target_pkg in {
                ("src", "services"),
                ("src", "managers"),
                ("web", "routers"),
            }:
                found.add(dotted)

        elif layer_dir == _SERVICES_DIR:
            if target_pkg in {("src", "managers"), ("web", "routers")}:
                found.add(dotted)
            elif target_pkg == ("src", "services"):
                # Other service units are forbidden, except:
                #   - imports within the same unit (not coupling)
                #   - dependency on service_factory (the DI seam; architecture.md)
                #   - service_factory itself may bundle all services
                unit = _target_unit(seg, 2)
                if (
                    unit
                    and unit != self_unit
                    and unit != _SERVICE_FACTORY
                    and not is_factory
                ):
                    found.add(dotted)

        elif layer_dir == _MANAGERS_DIR:
            if target_pkg == ("web", "routers"):
                found.add(dotted)
            elif target_pkg == ("src", "managers"):
                # Other managers are forbidden. shared/, components/ and the own unit are allowed.
                # components/ holds shared workflow parts used by several Managers; only the
                # forward Manager -> Component direction is allowed. Component -> Manager is
                # still flagged below because self_unit == "components" (keeps the direction forward).
                unit = _target_unit(seg, 2)
                if unit and unit not in {self_unit, "shared", "components"}:
                    found.add(dotted)

        elif layer_dir == _SHARED_DIR:
            if target_pkg in {("src", "services"), ("web", "routers")}:
                found.add(dotted)

        elif layer_dir == _ROUTERS_DIR:
            if target_pkg == ("src", "services"):
                # Allow only what is listed as a display-helper exception.
                unit = _target_unit(seg, 2)
                if unit not in _DISPLAY_HELPER_ALLOW:
                    found.add(dotted)

    return found


@pytest.mark.parametrize(
    "module_file, layer_dir",
    _layer_modules(),
    ids=lambda v: v.name if isinstance(v, Path) else v,
)
def test_no_reverse_dependency(module_file: Path, layer_dir: Path) -> None:
    """Each file has no import that violates the dependency direction (ratchet)."""
    found = _violations_for(module_file, layer_dir)
    key = str(module_file.relative_to(_SRC.parent))
    expected = _BASELINE.get(key, set())

    new_violations = found - expected
    assert not new_violations, (
        f"{key}: 依存方向に違反する import があります: {sorted(new_violations)}。"
        " architecture.md の依存方向（Router→Manager→Service, Models は全層参照可）に従い、"
        " 正しい層へ移してください。"
    )

    resolved = expected - found
    assert not resolved, (
        f"{key}: 解消済みの違反が _BASELINE に残っています: {sorted(resolved)}。"
        " tests/unit/test_architecture_deps.py の _BASELINE から削除してください。"
    )


def test_baseline_has_no_stale_entries() -> None:
    """No module path absent from _BASELINE remains."""
    known = {str(p.relative_to(_SRC.parent)) for p, _ in _layer_modules()}
    assert not set(_BASELINE) - known


_COMPONENTS_DIR = _MANAGERS_DIR / "components"


def _package_imports(directory: Path, package: list[str]) -> dict[str, set[str]]:
    """Module file -> modules of the same package it imports.

    ``package`` is the package's dotted path as segments, e.g.
    ``["src", "managers", "components"]``.
    """
    graph: dict[str, set[str]] = {}
    files = _py_files(directory)
    names = {p.stem for p in files}
    depth = len(package)
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        targets: set[str] = set()
        for dotted in _iter_imported_dotted(tree, path):
            seg = _segments(dotted)
            if seg[:depth] == package and len(seg) > depth:
                targets.add(seg[depth])
        # `from . import x` / `from src.managers.components import x` points at
        # the package itself, so treat the imported names as its modules
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                resolved = _resolve_import(node, path)
                if resolved and _segments(resolved) == package:
                    targets |= {alias.name for alias in node.names}
        graph[path.stem] = {t for t in targets if t in names and t != path.stem}
    return graph


def _component_imports(components_dir: Path) -> dict[str, set[str]]:
    """Component file -> Component files it imports (``src.managers.components.x``)."""
    return _package_imports(components_dir, ["src", "managers", "components"])


def _component_cycles(components_dir: Path) -> list[list[str]]:
    """Cycles in the Component import graph, each as ``[a, b, ..., a]``."""
    return _import_cycles(_component_imports(components_dir))


def _import_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Cycles in a module import graph, each as ``[a, b, ..., a]``."""
    cycles: list[list[str]] = []
    seen: set[frozenset[str]] = set()

    def visit(node: str, path: list[str]) -> None:
        for nxt in sorted(graph.get(node, ())):
            if nxt in path:
                cycle = path[path.index(nxt) :] + [nxt]
                key = frozenset(cycle)
                if key not in seen:
                    seen.add(key)
                    cycles.append(cycle)
                continue
            visit(nxt, path + [nxt])

    for start in sorted(graph):
        visit(start, [start])
    return cycles


def _modules_importing(directory: Path, name: str) -> list[str]:
    """File names under ``directory`` that import ``name`` from anywhere."""
    found: list[str] = []
    for path in _py_files(directory):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(
                alias.name == name for alias in node.names
            ):
                found.append(path.name)
                break
    return found


def test_components_have_no_import_cycle() -> None:
    """Components may depend on each other one-way, but must not form cycles.

    The layer checks above treat all of components/ as one unit and allow
    Component-to-Component imports unconditionally. Cycles are checked here on a per-file graph.
    """
    assert _component_cycles(_COMPONENTS_DIR) == []


_ADVANCED_DIR = _MANAGERS_DIR / "advanced_generation"
_ADVANCED_PACKAGE = ["src", "managers", "advanced_generation"]

#: Layer of each module of the advanced generation package (higher calls lower).
#: The Manager uses the stage classes, the stage classes share the run context,
#: and everything else is a leaf: types, errors and the pure helpers. A name
#: whose file does not exist is skipped; a file missing here fails the check.
_ADVANCED_LAYERS: dict[str, int] = {
    "manager": 3,
    "preparation": 2,
    "analysis_stage": 2,
    "finishing": 2,
    "controls": 2,
    "run_context": 1,
    "run_types": 0,
    "errors": 0,
    "documents": 0,
    "prompts": 0,
    "results": 0,
    "analysis_policy": 0,
    "analysis_verifier": 0,
    "detailed_analysis": 0,
    "join_planner": 0,
    "persona_check": 0,
    "segment_features": 0,
    "call_results": 0,
}
#: The only import allowed between two stage classes (the reader's end
#: goes through the finishing stage).
_ADVANCED_STAGE_PAIRS = frozenset({("controls", "finishing")})


def _advanced_layer_violations(directory: Path) -> list[tuple[str, str]]:
    """``(module, imported)`` pairs that go against the package's layers.

    Leaves may import each other (cycles are checked on their own); any other
    import must point at a lower layer, or be one of ``_ADVANCED_STAGE_PAIRS``.
    A module not in ``_ADVANCED_LAYERS`` is reported as ``(module, "")``.
    """
    found: list[tuple[str, str]] = []
    for module, targets in sorted(
        _package_imports(directory, _ADVANCED_PACKAGE).items()
    ):
        layer = _ADVANCED_LAYERS.get(module)
        if layer is None:
            found.append((module, ""))
            continue
        for target in sorted(targets):
            other = _ADVANCED_LAYERS.get(target)
            if other is None:
                found.append((module, target))
            elif other < layer or (layer == other == 0):
                continue
            elif (module, target) not in _ADVANCED_STAGE_PAIRS:
                found.append((module, target))
    return found


def test_advanced_generation_modules_follow_their_layers() -> None:
    """The advanced generation package has no import cycle and no upward import.

    The layer checks above treat all of ``advanced_generation/`` as one unit,
    so imports between its own modules are checked here on a per-file graph:
    Manager -> stage classes -> run context -> leaves, one way only.
    """
    graph = _package_imports(_ADVANCED_DIR, _ADVANCED_PACKAGE)
    assert _import_cycles(graph) == []
    assert _advanced_layer_violations(_ADVANCED_DIR) == []


def test_routers_do_not_import_access_target() -> None:
    """The material of an access decision (AccessTarget) is not returned to screens or APIs."""
    assert _modules_importing(_ROUTERS_DIR, "AccessTarget") == []


#: Code that runs only inside the analysis sandbox.
_SANDBOX_PACKAGE = "sandbox"


def _modules_importing_sandbox(*directories: Path) -> list[str]:
    """Files under ``directories`` that import the sandbox code by any form."""
    found: list[str] = []
    for directory in directories:
        for path in _py_files(directory, recursive=True):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                if any(n.split(".")[0] == _SANDBOX_PACKAGE for n in names):
                    found.append(path.name)
                    break
    return found


def test_app_does_not_import_sandbox_code() -> None:
    """Code under `sandbox/` runs only inside the session and is not imported by the app."""
    assert _modules_importing_sandbox(_SRC, _WEB) == []


class TestCheckerItself:
    """The check logic itself does not silently pass (required by error-catalog.md)."""

    def _write(self, tmp_path: Path, rel: str, source: str) -> Path:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
        return target

    def test_model_importing_service_is_flagged(self, tmp_path: Path) -> None:
        f = self._write(
            tmp_path,
            "src/models/foo.py",
            "from ..services.ai_service import AIService\n",
        )
        assert _violations_for(f, _MODELS_DIR)

    def test_router_importing_service_is_flagged(self, tmp_path: Path) -> None:
        f = self._write(
            tmp_path,
            "web/routers/foo.py",
            "from src.services.stores.persona_store import PersonaStore\n",
        )
        assert _violations_for(f, _ROUTERS_DIR)

    def test_router_display_helper_is_allowed(self, tmp_path: Path) -> None:
        f = self._write(
            tmp_path, "web/routers/foo.py", "from src.services import country_service\n"
        )
        assert not _violations_for(f, _ROUTERS_DIR)

    def test_service_importing_other_service_is_flagged(self, tmp_path: Path) -> None:
        f = self._write(
            tmp_path,
            "src/services/foo.py",
            "from ..services.ai_service import AIService\n",
        )
        assert _violations_for(f, _SERVICES_DIR)

    def test_service_importing_service_factory_is_allowed(self, tmp_path: Path) -> None:
        f = self._write(
            tmp_path,
            "src/services/foo.py",
            "from ..services.service_factory import get_service\n",
        )
        assert not _violations_for(f, _SERVICES_DIR)

    def test_store_importing_other_store_is_flagged(self, tmp_path: Path) -> None:
        # stores/ is one unit per table. Stores in the same directory are not coupled to each other.
        f = self._write(
            tmp_path,
            "src/services/stores/foo_store.py",
            "from .persona_store import PersonaStore\n",
        )
        assert _violations_for(f, _SERVICES_DIR)

    def test_store_importing_other_store_absolutely_is_flagged(
        self, tmp_path: Path
    ) -> None:
        f = self._write(
            tmp_path,
            "src/services/stores/foo_store.py",
            "from src.services.stores.persona_store import PersonaStore\n",
        )
        assert _violations_for(f, _SERVICES_DIR)

    def test_store_importing_models_is_allowed(self, tmp_path: Path) -> None:
        f = self._write(
            tmp_path,
            "src/services/stores/foo_store.py",
            "from ...models.persona import Persona\n",
        )
        assert not _violations_for(f, _SERVICES_DIR)

    def test_manager_importing_other_manager_is_flagged(self, tmp_path: Path) -> None:
        f = self._write(
            tmp_path,
            "src/managers/foo.py",
            "from ..managers.persona_manager import PersonaManager\n",
        )
        assert _violations_for(f, _MANAGERS_DIR)

    def test_manager_importing_shared_is_allowed(self, tmp_path: Path) -> None:
        f = self._write(
            tmp_path,
            "src/managers/foo.py",
            "from .shared.file_utils import compress_image\n",
        )
        assert not _violations_for(f, _MANAGERS_DIR)

    def test_manager_importing_component_is_allowed(self, tmp_path: Path) -> None:
        # Manager -> Component is forward, so legal.
        f = self._write(
            tmp_path,
            "src/managers/foo.py",
            "from .components.persona_agent_integration import PersonaAgentIntegration\n",
        )
        assert not _violations_for(f, _MANAGERS_DIR)

    def test_component_importing_manager_is_flagged(self, tmp_path: Path) -> None:
        # Component -> Manager is reverse. Files under components/ are classified in the managers layer
        # with self_unit="components", so an import of another manager unit is flagged.
        f = self._write(
            tmp_path,
            "src/managers/components/foo.py",
            "from ..persona_manager import PersonaManager\n",
        )
        assert _violations_for(f, _MANAGERS_DIR)

    def test_manager_importing_service_is_allowed(self, tmp_path: Path) -> None:
        # Manager -> service is forward, so legal.
        f = self._write(
            tmp_path,
            "src/managers/foo.py",
            "from ..services.ai_service import AIService\n",
        )
        assert not _violations_for(f, _MANAGERS_DIR)

    def test_model_importing_model_is_allowed(self, tmp_path: Path) -> None:
        f = self._write(tmp_path, "src/models/foo.py", "from .persona import Persona\n")
        assert not _violations_for(f, _MODELS_DIR)

    def test_component_cycle_is_flagged(self, tmp_path: Path) -> None:
        self._write(tmp_path, "src/managers/components/a.py", "from .b import B\n")
        self._write(
            tmp_path,
            "src/managers/components/b.py",
            "from src.managers.components.c import C\n",
        )
        self._write(tmp_path, "src/managers/components/c.py", "from . import a\n")
        cycles = _component_cycles(tmp_path / "src/managers/components")
        assert cycles == [["a", "b", "c", "a"]]

    def test_one_way_component_imports_are_allowed(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "src/managers/components/sharing.py",
            "from .access_policy import AccessPolicy\n",
        )
        self._write(
            tmp_path,
            "src/managers/components/access_policy.py",
            "from ...models.authz import Principal\n",
        )
        assert _component_cycles(tmp_path / "src/managers/components") == []

    def test_advanced_upward_and_unlisted_imports_are_flagged(
        self, tmp_path: Path
    ) -> None:
        package = "src/managers/advanced_generation"
        self._write(tmp_path, f"{package}/manager.py", "from .finishing import F\n")
        self._write(tmp_path, f"{package}/finishing.py", "from . import run_context\n")
        self._write(tmp_path, f"{package}/controls.py", "from .finishing import F\n")
        self._write(tmp_path, f"{package}/run_context.py", "from .errors import E\n")
        self._write(tmp_path, f"{package}/errors.py", "from .prompts import P\n")
        self._write(tmp_path, f"{package}/prompts.py", "x = 1\n")
        assert _advanced_layer_violations(tmp_path / package) == []
        self._write(tmp_path, f"{package}/results.py", "from .manager import M\n")
        self._write(tmp_path, f"{package}/extra.py", "x = 1\n")
        assert _advanced_layer_violations(tmp_path / package) == [
            ("extra", ""),
            ("results", "manager"),
        ]

    def test_importing_sandbox_is_flagged(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "src/services/foo.py",
            "from sandbox.persona_analysis import prepare\n",
        )
        self._write(tmp_path, "web/routers/bar.py", "import sandbox.persona_analysis\n")
        self._write(tmp_path, "src/models/baz.py", "from .sandbox_like import X\n")
        assert sorted(
            _modules_importing_sandbox(tmp_path / "src", tmp_path / "web")
        ) == ["bar.py", "foo.py"]

    def test_router_importing_access_target_is_flagged(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "web/routers/foo.py",
            "from src.models.authz import AccessTarget, Principal\n",
        )
        self._write(
            tmp_path, "web/routers/bar.py", "from src.models.authz import Principal\n"
        )
        assert _modules_importing(tmp_path / "web/routers", "AccessTarget") == [
            "foo.py"
        ]
