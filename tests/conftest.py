"""Shared test fixtures

Provides the common settings and fixtures needed to run tests in CI.
"""

import functools
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Generator, Iterable
from unittest.mock import Mock

import botocore.session
import pytest

# Force local login before importing web.main.
# Tests always run with local auth, even if the developer's shell sets another value.
os.environ["AUTH_MODE"] = "local"
os.environ.pop("ECS_CONTAINER_METADATA_URI_V4", None)
# Disable long-term memory, as CI (test.yml) does. If the shell has AgentCore Memory
# settings, tests with memory_service=None would build a real MemoryService.
os.environ["ENABLE_LONG_TERM_MEMORY"] = "false"
# Also drop the S3 bucket name, as CI does. If the shell has it, tests that do not
# inject a mock would write to real S3.
os.environ.pop("S3_BUCKET_NAME", None)
# Use the same values as CI for advanced generation (beta). Empty the Code Interpreter ID
# so tests without a mock never open a real session. CI sets no value for the table names
# or ENABLE_ADVANCED_GENERATION, so unset them to get the defaults (prefix-based names, true).
os.environ["CODE_INTERPRETER_ID"] = ""
os.environ.pop("ENABLE_ADVANCED_GENERATION", None)
os.environ.pop("SOURCE_FILES_TABLE_NAME", None)
os.environ.pop("ADVANCED_GENERATIONS_TABLE_NAME", None)
os.environ.pop("APP_SETTINGS_TABLE_NAME", None)

# Add the project root to the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models.authz import Principal, Role
from src.models.persona import Persona
from src.models.discussion import Discussion
from src.models.message import Message
from src.models.insight import Insight

# Reuse botocore's Loader (service definition loading) across sessions. moto discards
# boto3.DEFAULT_SESSION on every start, so each test's new session rebuilt the Loader and
# rescanned the data directory (about 35 ms per client, most of moto's test setup time).
# The Loader holds only read-only service definitions, so sharing it does not change results.
botocore.session.create_loader = functools.lru_cache(maxsize=None)(  # type: ignore[assignment]
    botocore.session.create_loader
)


def pytest_configure(config: pytest.Config) -> None:
    """Register markers that are not declared in pyproject.toml.

    ``ui`` and ``infra`` are registered here because pyproject.toml is a protected
    config file that is not edited by hand.
    """
    config.addinivalue_line(
        "markers",
        "ui: Component behaviour tests in a headless browser "
        "(Playwright + axe-core, no running app, no AWS)",
    )
    config.addinivalue_line(
        "markers",
        "infra: Deploy scripts and start-up / infrastructure settings "
        "(tests/infra/, fake aws and npx, no AWS)",
    )


# =============================================================================
# Directory fixtures
# =============================================================================


@pytest.fixture
def temp_upload_dir() -> Generator[str, None, None]:
    """Create a temporary upload directory."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        yield tmp_dir


# =============================================================================
# Model fixtures
# =============================================================================


@pytest.fixture
def sample_persona() -> Persona:
    """Provide a test persona."""
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
    """Provide a second test persona."""
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
    """Provide test messages."""
    return Message.create_new(
        persona_id="test-persona-id",
        persona_name="田中花子",
        content="私はマーケティングの観点から、顧客のニーズを深く理解することが重要だと思います。",
    )


@pytest.fixture
def sample_insight() -> Insight:
    """Provide test insights."""
    return Insight.create_new(
        category="顧客ニーズ",
        description="効率性を重視する顧客層が存在する",
        supporting_messages=["msg-1", "msg-2"],
        confidence_score=0.85,
    )


@pytest.fixture
def sample_discussion(sample_persona: Persona, sample_persona_2: Persona) -> Discussion:
    """Provide a test discussion."""
    discussion = Discussion.create_new(
        topic="新商品のマーケティング戦略について",
        participants=[sample_persona.id, sample_persona_2.id],
    )
    return discussion


@pytest.fixture
def sample_interview_text() -> str:
    """Provide a test interview text."""
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
# Mock service fixtures
# =============================================================================


@pytest.fixture
def mock_ai_service() -> Mock:
    """Provide a mocked AIService."""
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
    """Provide a mocked AgentService."""
    mock = Mock()

    # PersonaAgent mock
    mock_persona_agent = Mock()
    mock_persona_agent.get_persona_id.return_value = "test-persona-id"
    mock_persona_agent.get_persona_name.return_value = "テストペルソナ"
    mock_persona_agent.respond.return_value = "テスト応答"
    mock_persona_agent.dispose = Mock()

    mock.create_persona_agent.return_value = mock_persona_agent

    # FacilitatorAgent mock
    mock_facilitator = Mock()
    mock_facilitator.rounds = 1
    mock_facilitator.additional_instructions = ""
    mock_facilitator.invoke.return_value = "ラウンドの要約"
    mock_facilitator.dispose = Mock()

    mock.create_facilitator_agent.return_value = mock_facilitator

    return mock


@pytest.fixture
def mock_persona_store() -> Mock:
    """Provide a mocked PersonaStore."""
    from src.services.stores.persona_store import PersonaStore

    mock = Mock(spec=PersonaStore)
    mock.put_new.return_value = None
    mock.get.return_value = None
    return mock


@pytest.fixture
def file_manager(temp_upload_dir) -> Generator:
    """Provide a test FileManager."""
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
    """Provide a test PersonaManager."""
    from src.managers.persona_manager import PersonaManager

    manager = PersonaManager(persona_store=mock_persona_store)
    yield manager


# =============================================================================
# FastAPI test fixtures
# =============================================================================


@pytest.fixture
def test_app():
    """Provide a test FastAPI application."""
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
    """Return `login_as(client, name=..., roles=..., groups=...)`."""
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


#: The four users of the authorization tests: two members in different
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
    """Signed-in user in ``sales`` without any role."""
    return NO_ROLE


@pytest.fixture
def client(test_app) -> Generator:
    """Synchronous test client signed in as the dev user, with the CSRF header.

    Signed in by default so existing screen and API tests pass unchanged.
    Signed-out behavior is checked with a separate TestClient that has no cookie.
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
    """Provide an async test client."""
    try:
        from httpx import AsyncClient, ASGITransport

        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    except ImportError as e:
        pytest.skip(f"httpx not available: {e}")


# =============================================================================
# Environment fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset singletons after each test."""
    yield
    # Reset router singletons after each test
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
        # The two advanced generation (beta) Managers
        from web.routers import persona_advanced

        persona_advanced._source_upload_manager = None
        persona_advanced._advanced_generation_manager = None
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
        # The shared entry point and one shared part per task (holding a copy)
        from src.managers.components.sharing_runtime import reset_shared_parts
        from web.routers import sharing

        sharing._sharing_manager = None
        reset_shared_parts()
    except (ImportError, AttributeError):
        pass


@pytest.fixture
def env_dynamodb(monkeypatch):
    """Set the DynamoDB backend environment variables."""
    monkeypatch.setenv("DATABASE_BACKEND", "dynamodb")
    monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "Test")
    monkeypatch.setenv("DYNAMODB_REGION", "us-east-1")
