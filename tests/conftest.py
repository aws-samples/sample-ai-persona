"""
共通テストフィクスチャ

CI環境でのテスト実行に必要な共通設定とフィクスチャを提供します。
"""

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Generator, Iterable
from unittest.mock import Mock

import pytest

# web.main を import する前にログイン方式を手元専用にする（BR1.15、WF5 の 4）。
# 開発者のシェルに別の値があってもテストは常に local で動かす。
os.environ["AUTH_MODE"] = "local"
os.environ.pop("ECS_CONTAINER_METADATA_URI_V4", None)
# 長期記憶は CI（test.yml）と同じく無効にする。シェルに AgentCore Memory の設定が
# あると、memory_service=None のテストが本物の MemoryService を作ってしまうため。
os.environ["ENABLE_LONG_TERM_MEMORY"] = "false"
# S3 のバケット名も CI と同じく持たせない。シェルにあると、モックを渡していない
# テストが本物の S3 へ書き込みに行く。
os.environ.pop("S3_BUCKET_NAME", None)

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models.authz import Principal, Role
from src.models.persona import Persona
from src.models.discussion import Discussion
from src.models.message import Message
from src.models.insight import Insight


def pytest_configure(config: pytest.Config) -> None:
    """Register markers that are not declared in pyproject.toml.

    ``ui`` (U3) is registered here because pyproject.toml is a protected
    config file that is not edited by hand (team practices).
    """
    config.addinivalue_line(
        "markers",
        "ui: Component behaviour tests in a headless browser "
        "(Playwright + axe-core, no running app, no AWS)",
    )


# =============================================================================
# ディレクトリフィクスチャ
# =============================================================================


@pytest.fixture
def temp_upload_dir() -> Generator[str, None, None]:
    """一時的なアップロードディレクトリを作成"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        yield tmp_dir


# =============================================================================
# モデルフィクスチャ
# =============================================================================


@pytest.fixture
def sample_persona() -> Persona:
    """テスト用ペルソナを提供"""
    return Persona.create_new(
        name="田中花子",
        age=35,
        occupation="マーケティング担当",
        background="東京在住のマーケティング担当者。IT企業で10年の経験を持つ。",
        values=["効率性", "革新性", "チームワーク"],
        pain_points=["時間不足", "情報過多", "コスト意識"],
        goals=["キャリアアップ", "ワークライフバランス", "スキル向上"],
    )


@pytest.fixture
def sample_persona_2() -> Persona:
    """2人目のテスト用ペルソナを提供"""
    return Persona.create_new(
        name="佐藤太郎",
        age=28,
        occupation="商品開発者",
        background="大阪在住の商品開発者。スタートアップで5年の経験を持つ。",
        values=["品質", "顧客満足", "創造性"],
        pain_points=["予算制約", "技術的課題", "市場変化"],
        goals=["新商品開発", "市場拡大", "技術習得"],
    )


@pytest.fixture
def sample_message() -> Message:
    """テスト用メッセージを提供"""
    return Message.create_new(
        persona_id="test-persona-id",
        persona_name="田中花子",
        content="私はマーケティングの観点から、顧客のニーズを深く理解することが重要だと思います。",
    )


@pytest.fixture
def sample_insight() -> Insight:
    """テスト用インサイトを提供"""
    return Insight.create_new(
        category="顧客ニーズ",
        description="効率性を重視する顧客層が存在する",
        supporting_messages=["msg-1", "msg-2"],
        confidence_score=0.85,
    )


@pytest.fixture
def sample_discussion(sample_persona: Persona, sample_persona_2: Persona) -> Discussion:
    """テスト用議論を提供"""
    discussion = Discussion.create_new(
        topic="新商品のマーケティング戦略について",
        participants=[sample_persona.id, sample_persona_2.id],
    )
    return discussion


@pytest.fixture
def sample_interview_text() -> str:
    """テスト用インタビューテキストを提供"""
    return """
    インタビュー対象者: 田中花子さん（35歳、会社員）
    
    Q: 普段の生活について教えてください。
    A: 東京都内で一人暮らしをしています。マーケティング部で働いていて、
    新商品の企画や宣伝活動に携わっています。仕事は忙しいですが、
    やりがいを感じています。
    
    Q: 大切にしていることは何ですか？
    A: 効率性を重視しています。限られた時間の中で最大の成果を出したいと
    思っています。また、新しいことに挑戦することも大切にしています。
    
    Q: 現在抱えている課題はありますか？
    A: 時間管理が難しいです。仕事が忙しくて、プライベートの時間が
    なかなか取れません。また、情報が多すぎて、何を選択すべきか
    迷うことが多いです。
    
    Q: 将来の目標について教えてください。
    A: キャリアアップを目指しています。マネージャーになって、
    チームを率いてみたいです。また、ワークライフバランスを
    改善したいと思っています。
    """


# =============================================================================
# モックサービスフィクスチャ
# =============================================================================


@pytest.fixture
def mock_ai_service() -> Mock:
    """モック化されたAIServiceを提供"""
    mock = Mock()
    mock.generate_persona.return_value = Persona.create_new(
        name="生成されたペルソナ",
        age=30,
        occupation="テスト職業",
        background="テスト背景",
        values=["価値観1", "価値観2"],
        pain_points=["課題1", "課題2"],
        goals=["目標1", "目標2"],
    )
    mock.facilitate_discussion.return_value = [
        Message.create_new("p1", "ペルソナ1", "テストメッセージ1"),
        Message.create_new("p2", "ペルソナ2", "テストメッセージ2"),
    ]
    mock.extract_insights.return_value = [
        {
            "category": "テスト",
            "description": "テストインサイト",
            "confidence_score": 0.8,
        }
    ]
    return mock


@pytest.fixture
def mock_agent_service() -> Mock:
    """モック化されたAgentServiceを提供"""
    mock = Mock()

    # PersonaAgentのモック
    mock_persona_agent = Mock()
    mock_persona_agent.get_persona_id.return_value = "test-persona-id"
    mock_persona_agent.get_persona_name.return_value = "テストペルソナ"
    mock_persona_agent.respond.return_value = "テスト応答"
    mock_persona_agent.dispose = Mock()

    mock.create_persona_agent.return_value = mock_persona_agent

    # FacilitatorAgentのモック
    mock_facilitator = Mock()
    mock_facilitator.rounds = 1
    mock_facilitator.additional_instructions = ""
    mock_facilitator.invoke.return_value = "ラウンドの要約"
    mock_facilitator.dispose = Mock()

    mock.create_facilitator_agent.return_value = mock_facilitator

    return mock


@pytest.fixture
def mock_persona_store() -> Mock:
    """モック化されたPersonaStoreを提供"""
    from src.services.stores.persona_store import PersonaStore

    mock = Mock(spec=PersonaStore)
    mock.put_new.return_value = None
    mock.get.return_value = None
    return mock


@pytest.fixture
def file_manager(temp_upload_dir) -> Generator:
    """テスト用FileManagerを提供"""
    from src.managers.file_manager import FileManager
    from pathlib import Path
    from unittest.mock import Mock

    manager = FileManager(s3_service=Mock())
    manager.upload_dir = Path(temp_upload_dir)
    manager.knowledge_files_dir = Path(temp_upload_dir) / "knowledge_files"
    manager.knowledge_files_dir.mkdir(exist_ok=True)

    yield manager


@pytest.fixture
def persona_manager(mock_persona_store) -> Generator:
    """テスト用PersonaManagerを提供"""
    from src.managers.persona_manager import PersonaManager

    manager = PersonaManager(persona_store=mock_persona_store)
    yield manager


# =============================================================================
# FastAPIテスト用フィクスチャ
# =============================================================================


@pytest.fixture
def test_app():
    """テスト用FastAPIアプリケーションを提供"""
    try:
        from web.main import app

        return app
    except ImportError as e:
        pytest.skip(f"FastAPI or dependencies not available: {e}")


def login_as(
    client: Any,
    *,
    name: str | None = "開発ユーザー",
    email: str | None = "dev@example.com",
    roles: Iterable[str] = ("member",),
    groups: Iterable[str] = (),
) -> None:
    """Log the test client in as a local development user.

    Creates a ``local`` LoginSession through the running AuthManager (the
    same path ``GET /login`` uses under ``AUTH_MODE=local``) and stores its
    session cookie on the client, replacing any previous login.
    """
    from web.auth_middleware import SESSION_COOKIE_NAME
    from web.routers.auth import get_auth_manager

    session_id = get_auth_manager().start_local_session(
        display_name=name,
        email=email,
        roles=frozenset(roles),
        group_ids=frozenset(groups),
    )
    client.cookies.set(SESSION_COOKIE_NAME, session_id)


@pytest.fixture(name="login_as")
def login_as_fixture() -> Callable[..., None]:
    """`login_as(client, name=..., roles=..., groups=...)` を返す。"""
    return login_as


@dataclass(frozen=True)
class SampleUser:
    """A test user that can sign in through :func:`login_as` or act as a Principal.

    The ``sub`` matches what ``AuthManager.start_local_session`` assigns
    (``local:<email>``), so API tests and Manager tests see the same identity.
    """

    name: str
    email: str
    roles: tuple[str, ...]
    groups: tuple[str, ...]

    @property
    def sub(self) -> str:
        """The Principal ``sub`` of this user under ``AUTH_MODE=local``."""
        return f"local:{self.email}"

    def principal(self) -> Principal:
        """Build the Principal ``AuthMiddleware`` would put on the request."""
        return Principal(
            sub=self.sub,
            roles=frozenset(Role(role) for role in self.roles),
            group_ids=frozenset(self.groups),
            display_name=self.name,
            email=self.email,
        )

    def login(self, client: Any) -> None:
        """Sign ``client`` in as this user (replaces any previous login)."""
        login_as(
            client,
            name=self.name,
            email=self.email,
            roles=self.roles,
            groups=self.groups,
        )


#: The four users of the authorization tests (U2): two members in different
#: departments, an admin, and a user without any role.
MEMBER_A = SampleUser("メンバーA", "member-a@example.com", ("member",), ("sales",))
MEMBER_B = SampleUser("メンバーB", "member-b@example.com", ("member",), ("dev",))
ADMIN = SampleUser("管理者", "admin@example.com", ("admin",), ())
NO_ROLE = SampleUser("ロールなし", "no-role@example.com", (), ("sales",))


@pytest.fixture
def member_a() -> SampleUser:
    """Member in the ``sales`` department."""
    return MEMBER_A


@pytest.fixture
def member_b() -> SampleUser:
    """Member in the ``dev`` department (unrelated to member A)."""
    return MEMBER_B


@pytest.fixture
def admin_user() -> SampleUser:
    """Admin without any department."""
    return ADMIN


@pytest.fixture
def no_role_user() -> SampleUser:
    """Signed-in user in ``sales`` without any role (FR1.7)."""
    return NO_ROLE


@pytest.fixture
def client(test_app) -> Generator:
    """ログイン済み（開発用の利用者）の同期テストクライアント（CSRF対策ヘッダー付き）。

    既存の画面・API のテストを変えずに通すため、既定でログイン済みにする。
    未ログインの振る舞いは Cookie を持たない TestClient を個別に作って確かめる。
    """
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        pytest.skip(f"FastAPI TestClient not available: {e}")

    with TestClient(test_app, headers={"HX-Request": "true"}) as c:
        login_as(c)
        yield c


@pytest.fixture
async def async_client(test_app):
    """非同期テストクライアントを提供"""
    try:
        from httpx import AsyncClient, ASGITransport

        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    except ImportError as e:
        pytest.skip(f"httpx not available: {e}")


# =============================================================================
# 環境設定フィクスチャ
# =============================================================================


@pytest.fixture(autouse=True)
def reset_singletons():
    """各テスト後にシングルトンをリセット"""
    yield
    # テスト後にルーターのシングルトンをリセット
    try:
        from web.routers import api, persona, discussion, interview

        api._persona_manager = None
        api._discussion_manager = None
        api._job_manager = None
        api._persona_generation_manager = None
        api._agent_discussion_manager = None
        api._interview_manager = None
        persona._persona_manager = None
        persona._persona_memory_manager = None
        persona._file_manager = None
        persona._persona_generation_manager = None
        discussion._persona_manager = None
        discussion._discussion_manager = None
        discussion._agent_discussion_manager = None
        discussion._report_manager = None
        discussion._file_manager = None
        interview._persona_manager = None
        interview._interview_manager = None
        interview._file_manager = None
    except (ImportError, AttributeError):
        pass
    try:
        from web.routers import survey

        survey._template_manager = None
        survey._dataset_manager = None
        survey._execution_manager = None
        survey._analysis_manager = None
        survey._file_manager = None
    except (ImportError, AttributeError):
        pass
    try:
        from web.routers import settings

        settings._dataset_manager = None
        settings._settings_manager = None
    except (ImportError, AttributeError):
        pass
    try:
        from web.routers import auth

        auth._auth_manager = None
    except (ImportError, AttributeError):
        pass
    try:
        # 共有の入口（U4）と、タスクごとに 1 つの共有の部品（写しを持つ）
        from src.managers.components.sharing_runtime import reset_shared_parts
        from web.routers import sharing

        sharing._sharing_manager = None
        reset_shared_parts()
    except (ImportError, AttributeError):
        pass


@pytest.fixture
def env_dynamodb(monkeypatch):
    """DynamoDBバックエンドの環境変数を設定"""
    monkeypatch.setenv("DATABASE_BACKEND", "dynamodb")
    monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "Test")
    monkeypatch.setenv("DYNAMODB_REGION", "us-east-1")
