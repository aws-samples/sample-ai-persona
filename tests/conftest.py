"""
共通テストフィクスチャ

CI環境でのテスト実行に必要な共通設定とフィクスチャを提供します。
"""

import sys
import tempfile
from pathlib import Path
from typing import Generator
from unittest.mock import Mock

import pytest

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models.persona import Persona
from src.models.discussion import Discussion
from src.models.message import Message
from src.models.insight import Insight


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
    mock.generate_persona_system_prompt.return_value = "テスト用システムプロンプト"

    # PersonaAgentのモック
    mock_persona_agent = Mock()
    mock_persona_agent.get_persona_id.return_value = "test-persona-id"
    mock_persona_agent.get_persona_name.return_value = "テストペルソナ"
    mock_persona_agent.respond.return_value = "テスト応答"
    mock_persona_agent.dispose = Mock()

    mock.create_persona_agent.return_value = mock_persona_agent

    # FacilitatorAgentのモック
    mock_facilitator = Mock()
    mock_facilitator.should_continue.return_value = False
    mock_facilitator.dispose = Mock()

    mock.create_facilitator_agent.return_value = mock_facilitator

    return mock


@pytest.fixture
def mock_database_service() -> Mock:
    """モック化されたDatabaseServiceを提供"""
    mock = Mock()
    mock.save_persona.return_value = "test-persona-id"
    mock.get_persona.return_value = None
    mock.get_all_personas.return_value = []
    mock.save_discussion.return_value = "test-discussion-id"
    mock.get_discussion.return_value = None
    mock.get_discussions.return_value = []
    return mock


@pytest.fixture
def file_manager(temp_upload_dir) -> Generator:
    """テスト用FileManagerを提供"""
    from src.managers.file_manager import FileManager
    from pathlib import Path
    from unittest.mock import Mock

    # モックDBサービスを使用してAWS接続を回避
    mock_db = Mock()
    manager = FileManager(db_service=mock_db)
    manager.upload_dir = Path(temp_upload_dir)
    manager.knowledge_files_dir = Path(temp_upload_dir) / "knowledge_files"
    manager.knowledge_files_dir.mkdir(exist_ok=True)

    yield manager


@pytest.fixture
def persona_manager(mock_database_service) -> Generator:
    """テスト用PersonaManagerを提供"""
    from src.managers.persona_manager import PersonaManager

    manager = PersonaManager(database_service=mock_database_service)
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


@pytest.fixture
def client(test_app) -> Generator:
    """同期テストクライアントを提供（CSRF対策ヘッダー付き）"""
    try:
        from fastapi.testclient import TestClient

        with TestClient(test_app, headers={"HX-Request": "true"}) as c:
            yield c
    except ImportError as e:
        pytest.skip(f"FastAPI TestClient not available: {e}")


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
        persona._persona_manager = None
        persona._file_manager = None
        discussion._persona_manager = None
        discussion._discussion_manager = None
        discussion._agent_discussion_manager = None
        discussion._file_manager = None
        interview._persona_manager = None
        interview._interview_manager = None
    except (ImportError, AttributeError):
        pass
    try:
        from web.routers import survey

        survey._survey_manager = None
    except (ImportError, AttributeError):
        pass
    try:
        from web.routers import settings

        settings._dataset_manager = None
    except (ImportError, AttributeError):
        pass


@pytest.fixture
def env_dynamodb(monkeypatch):
    """DynamoDBバックエンドの環境変数を設定"""
    monkeypatch.setenv("DATABASE_BACKEND", "dynamodb")
    monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "Test")
    monkeypatch.setenv("DYNAMODB_REGION", "us-east-1")
