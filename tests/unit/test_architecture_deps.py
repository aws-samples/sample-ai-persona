"""層の依存方向を機械検査する（`.claude/rules/architecture.md` を単一ソースとする）。

pre-push-review の「アーキテクチャ違反チェック」は元々LLMが architecture.md を読んで
差分に当てはめる単発判断であり、再現性も実行保証もなかった。依存方向は import を
走査するだけの決定的なルールなので、error-catalog.md が確立した「規律は人手レビュー
ではなくAST走査で保証する」方式（`tests/api/test_error_exposure.py`）に倣って機械化する。

検査する依存方向（architecture.md §依存方向）:
- Router → Manager → Service。Models は全層から参照可。逆方向禁止。
- Models   : services / managers / routers を import しない
- Service  : managers / routers を import しない。他の service も不可（`service_factory` は例外）
- Manager  : routers を import しない。他の manager も不可（`shared/` は可）
- Shared   : services / routers を import しない
- Router   : services を直接 import しない
             （例外: テンプレート表示ヘルパー。architecture.md 31行。`_DISPLAY_HELPER_ALLOW`）

`_BASELINE` と完全一致するラチェット方式をとる。現状のコードは違反ゼロなので baseline は
空であり、逆方向の import が1件でも入れば失敗する。責務配置チェック（Manager に I/O、
Service にビジネスルール等）は静的走査になじまない主観判断のため、この検査の対象外
（pre-push-review 側で WARN として人手レビューが担当する）。
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SRC = Path(__file__).parent.parent.parent / "src"
_WEB = Path(__file__).parent.parent.parent / "web"

# 各層のディレクトリと、その層が import してはならない層（禁止先）。
# 禁止先は「モジュールパスの先頭セグメント列」で表す（相対・絶対の両方を解決後に判定）。
_MODELS_DIR = _SRC / "models"
_SERVICES_DIR = _SRC / "services"
_MANAGERS_DIR = _SRC / "managers"
_SHARED_DIR = _SRC / "managers" / "shared"
_ROUTERS_DIR = _WEB / "routers"

# Service が他の Service を import してよい唯一の例外（architecture.md 8行）。
_SERVICE_FACTORY = "service_factory"

# Router がテンプレート表示ヘルパーとして直接 import してよい Service（architecture.md 31行）。
# 純粋なデータ参照（ISO国コード→名前等）でビジネスロジックを含まないもののみ。
_DISPLAY_HELPER_ALLOW = {"country_service"}

# 既知の違反（module 相対パス -> 違反 import 先の集合）。新たな逆方向 import は
# ここに足すのではなく、正しい層へ移すこと。ラチェットとして、ここに載っていない
# 新規違反は fail し、解消済みで残っているエントリも fail する。
#
# agent_service は実行時に兄弟 service を遅延 import して結合している（AgentService が
# ツール生成のために DataAgent / MCP / KnowledgeBase / Memory を直接束ねる構造）。
# 本来は Manager 層でオーケストレートすべき既存負債。解消したらこのエントリを削除する。
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
    """(ファイルパス, 所属層ディレクトリ) の一覧。shared は managers と別扱い。"""
    result: list[tuple[Path, Path]] = []
    result += [(p, _MODELS_DIR) for p in _py_files(_MODELS_DIR, recursive=True)]
    result += [(p, _SERVICES_DIR) for p in _py_files(_SERVICES_DIR, recursive=True)]
    result += [(p, _ROUTERS_DIR) for p in _py_files(_ROUTERS_DIR, recursive=True)]
    # managers 直下（shared を除く）と shared を分ける。
    shared_files = set(_py_files(_SHARED_DIR, recursive=True))
    for p in _py_files(_MANAGERS_DIR, recursive=True):
        if p in shared_files:
            result.append((p, _SHARED_DIR))
        else:
            result.append((p, _MANAGERS_DIR))
    return result


def _resolve_import(node: ast.AST, module_file: Path) -> str | None:
    """import 文を `src.services.foo` / `web.routers.bar` 形式のドット表記へ正規化する。

    相対 import（`from ..services.x`）は module_file の位置から解決する。
    解決できない・パッケージ外なら None。
    """
    if isinstance(node, ast.Import):
        # `import src.services.x` 形式。alias は呼び出し側で個別に扱う。
        return None
    if not isinstance(node, ast.ImportFrom):
        return None

    if node.level == 0:
        return node.module

    # 相対 import。module_file が属するパッケージ（src / web）を起点に解決する。
    # src/managers/foo.py の親は src/managers、level=2 の `..services` は src/services。
    parts = module_file.parent.parts
    # プロジェクトルート配下の "src" or "web" 以降だけを使う。
    for anchor in ("src", "web"):
        if anchor in parts:
            idx = parts.index(anchor)
            base = list(parts[idx:])
            break
    else:
        return None

    # level=1 は現ディレクトリ、level=2 は1つ上。level-1 個だけ末尾を落とす。
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
    """ドット表記の import 先を列挙する。相対・絶対の両方を解決する。

    `from src.services import country_service` のように module が層パッケージそのものを
    指す場合は、import された各名前を submodule とみなして展開する
    （`src.services.country_service`）。module が具体モジュールを指す場合の import 名は
    クラス・関数なので展開しない。
    """
    type_checking_imports = _type_checking_import_nodes(tree)
    out: list[str] = []
    for node in ast.walk(tree):
        # `if TYPE_CHECKING:` 配下の import は型注釈専用で実行時の結合を生まない。
        # 依存方向は実行時ロードの向きで判定するため対象外とする。
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
    """`if TYPE_CHECKING:` ブロック直下の import 文ノードを集める。"""
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
    """`src.services.ai_service` -> ['src','services','ai_service']（先頭 src/web に正規化）。"""
    parts = dotted.split(".")
    for anchor in ("src", "web"):
        if anchor in parts:
            return parts[parts.index(anchor) :]
    return parts


def _unit_of(module_file: Path, layer_dir: Path) -> str:
    """層内でのユニット名。サブパッケージ（`memory/` 等）はディレクトリ名を、直下の
    モジュールはファイル名（拡張子なし）を返す。同一ユニット内の import は結合ではない。

    パスセグメント名で解決するため、実ファイルでも一時ファイルでも動く
    （`relative_to` はテスト用の tmp_path 配下で例外になるため使わない）。"""
    key = layer_dir.name  # models / services / managers / shared / routers
    parts = module_file.parts
    if key in parts:
        after = parts[parts.index(key) + 1 :]
        if len(after) > 1:  # サブパッケージ配下（after = [subpkg, ..., file.py]）
            return after[0]
    return module_file.stem


def _target_unit(seg: list[str], layer_index: int) -> str:
    """import 先ドット表記から、その層内のユニット名を取り出す。

    seg=['src','services','memory','memory_service'], layer_index=2 -> 'memory'
    seg=['src','services','country_service'],          layer_index=2 -> 'country_service'
    """
    after = seg[layer_index:]
    return after[0] if after else ""


def _violations_for(module_file: Path, layer_dir: Path) -> set[str]:
    """1ファイルの逆方向 import 違反を集める。返すのは違反した import 先（ドット表記）。

    同一層内は「ユニット」単位で判定する。ユニットは層直下のモジュール名、または
    サブパッケージ名（`memory/` 等）。同一ユニット内の import は結合ではないので許可する。
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
                # 他の service ユニットは禁止。ただし以下は除外:
                #   - 自ユニット内の import（結合ではない）
                #   - service_factory への依存（DI の継ぎ目。architecture.md 8行）
                #   - service_factory 自身は全 service を束ねてよい
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
                # 他の manager は禁止。shared/ と自ユニットは可。
                unit = _target_unit(seg, 2)
                if unit and unit not in {self_unit, "shared"}:
                    found.add(dotted)

        elif layer_dir == _SHARED_DIR:
            if target_pkg in {("src", "services"), ("web", "routers")}:
                found.add(dotted)

        elif layer_dir == _ROUTERS_DIR:
            if target_pkg == ("src", "services"):
                # 表示ヘルパー例外に載っているものだけ許可。
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
    """各ファイルが依存方向に違反する import を持たないこと（ラチェット方式）。"""
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
    """_BASELINE に存在しないモジュールパスが残っていないこと。"""
    known = {str(p.relative_to(_SRC.parent)) for p, _ in _layer_modules()}
    assert not set(_BASELINE) - known


class TestCheckerItself:
    """検査ロジック自体が空振りしていないことの検証（error-catalog.md の要求）。"""

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
            "from src.services.database_service import DatabaseService\n",
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

    def test_manager_importing_service_is_allowed(self, tmp_path: Path) -> None:
        # manager → service は順方向。合法。
        f = self._write(
            tmp_path,
            "src/managers/foo.py",
            "from ..services.ai_service import AIService\n",
        )
        assert not _violations_for(f, _MANAGERS_DIR)

    def test_model_importing_model_is_allowed(self, tmp_path: Path) -> None:
        f = self._write(tmp_path, "src/models/foo.py", "from .persona import Persona\n")
        assert not _violations_for(f, _MODELS_DIR)
