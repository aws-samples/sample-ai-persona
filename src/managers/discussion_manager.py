"""
Discussion Manager for AI Persona System.
Handles discussion setup, progress management, and insight generation functionality.
"""

import logging
from typing import List, Optional, Dict, Any
from pathlib import Path
from datetime import datetime

from ..models.persona import Persona
from ..models.discussion import Discussion
from ..models.insight import Insight
from ..models.insight_category import InsightCategory
from ..services.ai_service import AIService, AIServiceError
from ..services.database_service import DatabaseService, DatabaseError
from ..services.service_factory import service_factory


class DiscussionManagerError(Exception):
    """Custom exception for discussion manager related errors."""

    pass


class DiscussionManager:
    """
    Manager class for handling discussion-related operations.
    Orchestrates discussion setup, progress management, and insight generation.
    """

    def __init__(
        self,
        ai_service: AIService | None = None,
        database_service: Optional[DatabaseService] = None,
    ):
        """
        Initialize discussion manager.

        Args:
            ai_service: AI service instance for discussion facilitation (optional, uses singleton if not provided)
            database_service: Database service instance for persistence (optional, uses singleton if not provided)
        """
        self.logger = logging.getLogger(__name__)

        # Use singleton services if not provided
        self.ai_service = ai_service or service_factory.get_ai_service()
        self.database_service = (
            database_service or service_factory.get_database_service()
        )

    def start_discussion(
        self,
        personas: List[Persona],
        topic: str,
        document_ids: Optional[List[str]] = None,
    ) -> Discussion:
        """
        Start a discussion between selected personas on a given topic.

        This method implements the complete discussion workflow:
        1. Validates input parameters (personas and topic)
        2. Loads document files if document_ids provided
        3. Creates a new discussion instance
        4. Uses AI service to facilitate discussion between personas
        5. Returns the discussion with generated messages

        Args:
            personas: List of Persona objects participating in the discussion
            topic: Discussion topic string
            document_ids: Optional list of document IDs to include in discussion

        Returns:
            Discussion: Discussion object with generated messages

        Raises:
            DiscussionManagerError: If discussion start fails
        """
        # Validate input parameters
        self._validate_discussion_input(personas, topic)

        # Load and validate documents if provided
        documents_data = None
        documents_metadata = None
        if document_ids:
            documents_data, documents_metadata = self._load_documents(document_ids)

        self.logger.info(
            f"Starting discussion with {len(personas)} personas on topic: '{topic[:50]}...'"
        )

        try:
            # Create new discussion instance with documents metadata
            discussion = Discussion.create_new(
                topic=topic.strip(),
                participants=[persona.id for persona in personas],
                documents=documents_metadata,
            )

            # Generate discussion messages using AI service
            messages = self.ai_service.facilitate_discussion(
                personas, topic.strip(), documents=documents_data
            )

            # Add messages to discussion
            for message in messages:
                discussion = discussion.add_message(message)

            # Validate discussion results
            self._validate_discussion_results(discussion, personas)

            self.logger.info(
                f"Discussion started successfully: {discussion.id} with {len(messages)} messages"
            )
            return discussion

        except AIServiceError as e:
            error_msg = f"AI service error during discussion: {e}"
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error during discussion start: {e}"
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)

    def generate_insights(
        self, discussion: Discussion, categories: Optional[List[InsightCategory]] = None
    ) -> List[Insight]:
        """
        Generate insights from discussion messages.

        Args:
            discussion: Discussion object containing messages
            categories: Optional list of insight categories (uses default if None)

        Returns:
            List[Insight]: Generated insights from the discussion

        Raises:
            DiscussionManagerError: If insight generation fails
        """
        if not discussion:
            raise DiscussionManagerError("議論オブジェクトが無効です")

        if not discussion.messages or len(discussion.messages) < 2:
            raise DiscussionManagerError(
                "インサイト生成には最低2つのメッセージが必要です"
            )

        self.logger.info(
            f"Generating insights for discussion: {discussion.id} with {len(discussion.messages)} messages"
        )

        try:
            # Extract insights using AI service (now returns structured data)
            insight_data_list = self.ai_service.extract_insights(
                discussion.messages, categories=categories, topic=discussion.topic
            )

            # Convert structured data to Insight objects
            insights = self._parse_insights_from_structured_data(insight_data_list)

            # Validate generated insights
            self._validate_generated_insights(insights)

            self.logger.info(
                f"Generated {len(insights)} insights for discussion: {discussion.id}"
            )
            return insights

        except AIServiceError as e:
            error_msg = f"AI service error during insight generation: {e}"
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error during insight generation: {e}"
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)

    def save_discussion(self, discussion: Discussion) -> str:
        """
        Save a discussion to the database.

        Args:
            discussion: Discussion object to save

        Returns:
            str: The discussion ID

        Raises:
            DiscussionManagerError: If save operation fails
        """
        if not discussion:
            raise DiscussionManagerError("議論オブジェクトが無効です")

        # Validate discussion before saving
        self._validate_discussion_for_save(discussion)

        try:
            discussion_id = self.database_service.save_discussion(discussion)
            self.logger.info(
                f"Discussion saved successfully: {discussion.topic} (ID: {discussion_id})"
            )
            return discussion_id

        except DatabaseError as e:
            error_msg = f"Database error while saving discussion: {e}"
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error while saving discussion: {e}"
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)

    def save_discussion_with_insights(
        self, discussion: Discussion, insights: List[Insight]
    ) -> str:
        """
        Save a discussion with generated insights to the database.

        Args:
            discussion: Discussion object to save
            insights: List of insights to add to the discussion

        Returns:
            str: The discussion ID

        Raises:
            DiscussionManagerError: If save operation fails
        """
        if not discussion:
            raise DiscussionManagerError("議論オブジェクトが無効です")

        if not insights:
            raise DiscussionManagerError("インサイトが無効です")

        try:
            # Add insights to discussion
            discussion_with_insights = discussion
            for insight in insights:
                discussion_with_insights = discussion_with_insights.add_insight(insight)

            # Save the complete discussion
            discussion_id = self.save_discussion(discussion_with_insights)

            self.logger.info(
                f"Discussion with {len(insights)} insights saved successfully: {discussion_id}"
            )
            return discussion_id

        except DiscussionManagerError:
            # Re-raise DiscussionManagerError
            raise
        except Exception as e:
            error_msg = f"Unexpected error while saving discussion with insights: {e}"
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)

    def get_discussion(self, discussion_id: str) -> Optional[Discussion]:
        """
        Retrieve a discussion by ID.

        Args:
            discussion_id: ID of the discussion to retrieve

        Returns:
            Discussion object if found, None otherwise

        Raises:
            DiscussionManagerError: If retrieval operation fails
        """
        if not discussion_id or not discussion_id.strip():
            raise DiscussionManagerError("議論IDが無効です")

        try:
            discussion = self.database_service.get_discussion(discussion_id.strip())
            if discussion:
                self.logger.debug(
                    f"Discussion retrieved successfully: {discussion.topic} (ID: {discussion_id})"
                )
            else:
                self.logger.debug(f"Discussion not found: {discussion_id}")
            return discussion

        except DatabaseError as e:
            error_msg = f"Database error while retrieving discussion: {e}"
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error while retrieving discussion: {e}"
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)

    def get_discussion_history(self) -> List[Discussion]:
        """
        Retrieve all discussions from the database ordered by creation date.

        Returns:
            List of Discussion objects ordered by creation date (newest first)

        Raises:
            DiscussionManagerError: If retrieval operation fails
        """
        try:
            discussions = self.database_service.get_discussions()
            self.logger.info(f"Retrieved {len(discussions)} discussions from database")
            return discussions

        except DatabaseError as e:
            error_msg = f"Database error while retrieving discussion history: {e}"
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error while retrieving discussion history: {e}"
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)

    def get_discussions_by_topic(self, topic_pattern: str) -> List[Discussion]:
        """
        Search discussions by topic pattern.

        Args:
            topic_pattern: Topic pattern to search for

        Returns:
            List of matching Discussion objects

        Raises:
            DiscussionManagerError: If search operation fails
        """
        if not topic_pattern or not topic_pattern.strip():
            return []

        try:
            discussions = self.database_service.get_discussions_by_topic(
                topic_pattern.strip()
            )
            self.logger.info(
                f"Found {len(discussions)} discussions matching topic pattern: '{topic_pattern}'"
            )
            return discussions

        except DatabaseError as e:
            error_msg = f"Database error while searching discussions by topic: {e}"
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error while searching discussions by topic: {e}"
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)

    def get_discussions_by_participant(self, persona_id: str) -> List[Discussion]:
        """
        Get discussions that include a specific persona as participant.

        Args:
            persona_id: ID of the persona to search for

        Returns:
            List of Discussion objects where the persona participated

        Raises:
            DiscussionManagerError: If search operation fails
        """
        if not persona_id or not persona_id.strip():
            raise DiscussionManagerError("ペルソナIDが無効です")

        try:
            discussions = self.database_service.get_discussions_by_participant(
                persona_id.strip()
            )
            self.logger.info(
                f"Found {len(discussions)} discussions with participant: {persona_id}"
            )
            return discussions

        except DatabaseError as e:
            error_msg = (
                f"Database error while searching discussions by participant: {e}"
            )
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)
        except Exception as e:
            error_msg = (
                f"Unexpected error while searching discussions by participant: {e}"
            )
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)

    def delete_discussion(self, discussion_id: str) -> bool:
        """
        Delete a discussion from the database.

        Args:
            discussion_id: ID of the discussion to delete

        Returns:
            True if deletion was successful, False if discussion not found

        Raises:
            DiscussionManagerError: If deletion operation fails
        """
        if not discussion_id or not discussion_id.strip():
            raise DiscussionManagerError("議論IDが無効です")

        try:
            success = self.database_service.delete_discussion(discussion_id.strip())
            if success:
                self.logger.info(f"Discussion deleted successfully: {discussion_id}")
            else:
                self.logger.warning(
                    f"Discussion not found for deletion: {discussion_id}"
                )
            return success

        except DatabaseError as e:
            error_msg = f"Database error while deleting discussion: {e}"
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error while deleting discussion: {e}"
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)

    def regenerate_insights(
        self, discussion_id: str, categories: Optional[List[InsightCategory]] = None
    ) -> List[Insight]:
        """
        Regenerate insights for an existing discussion with new categories.

        This method:
        1. Retrieves the existing discussion
        2. Generates new insights with the specified categories
        3. Updates the discussion with new insights (replacing old ones)
        4. Saves categories to discussion config if provided
        5. Saves the updated discussion to database

        Args:
            discussion_id: ID of the discussion to regenerate insights for
            categories: Optional list of insight categories (uses default if None)

        Returns:
            List[Insight]: Newly generated insights

        Raises:
            DiscussionManagerError: If regeneration fails or discussion not found
        """
        if not discussion_id or not discussion_id.strip():
            raise DiscussionManagerError("議論IDが無効です")

        self.logger.info(f"Regenerating insights for discussion: {discussion_id}")

        try:
            # Retrieve existing discussion
            discussion = self.get_discussion(discussion_id.strip())
            if not discussion:
                raise DiscussionManagerError(f"議論が見つかりません: {discussion_id}")

            # Generate new insights with specified categories
            new_insights = self.generate_insights(discussion, categories=categories)

            # Create new discussion with updated insights (replacing old ones)
            updated_discussion = Discussion(
                id=discussion.id,
                topic=discussion.topic,
                participants=discussion.participants,
                messages=discussion.messages,
                insights=new_insights,  # Replace with new insights
                created_at=discussion.created_at,
                mode=discussion.mode,
                agent_config=discussion.agent_config,
            )

            # Save categories to config if provided
            if categories:
                updated_discussion = self._save_categories_to_config(
                    updated_discussion, categories
                )

            # Save updated discussion to database
            self.database_service.save_discussion(updated_discussion)

            self.logger.info(
                f"Insights regenerated successfully: {discussion_id} with {len(new_insights)} insights"
            )
            return new_insights

        except DiscussionManagerError:
            # Re-raise DiscussionManagerError
            raise
        except Exception as e:
            error_msg = f"Unexpected error during insight regeneration: {e}"
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)

    def update_discussion_insights(
        self, discussion_id: str, insights: List[Insight]
    ) -> bool:
        """
        Update insights for an existing discussion.

        Args:
            discussion_id: ID of the discussion to update
            insights: List of Insight objects to save

        Returns:
            True if update was successful, False if discussion not found

        Raises:
            DiscussionManagerError: If update operation fails
        """
        if not discussion_id or not discussion_id.strip():
            raise DiscussionManagerError("議論IDが無効です")

        if not insights:
            raise DiscussionManagerError("インサイトが無効です")

        # Validate insights
        self._validate_generated_insights(insights)

        try:
            success = self.database_service.update_discussion_insights(
                discussion_id.strip(), insights
            )
            if success:
                self.logger.info(
                    f"Discussion insights updated successfully: {discussion_id} with {len(insights)} insights"
                )
            else:
                self.logger.warning(
                    f"Discussion not found for insight update: {discussion_id}"
                )
            return success

        except DatabaseError as e:
            error_msg = f"Database error while updating discussion insights: {e}"
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error while updating discussion insights: {e}"
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)

    def get_discussion_count(self) -> int:
        """
        Get the total number of discussions in the database.

        Returns:
            Number of discussions

        Raises:
            DiscussionManagerError: If count operation fails
        """
        try:
            count = self.database_service.get_discussion_count()
            self.logger.debug(f"Discussion count: {count}")
            return count

        except DatabaseError as e:
            error_msg = f"Database error while getting discussion count: {e}"
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error while getting discussion count: {e}"
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)

    def discussion_exists(self, discussion_id: str) -> bool:
        """
        Check if a discussion exists in the database.

        Args:
            discussion_id: ID of the discussion to check

        Returns:
            True if discussion exists, False otherwise

        Raises:
            DiscussionManagerError: If check operation fails
        """
        if not discussion_id or not discussion_id.strip():
            return False

        try:
            exists = self.database_service.discussion_exists(discussion_id.strip())
            self.logger.debug(f"Discussion exists check for {discussion_id}: {exists}")
            return exists

        except DatabaseError as e:
            error_msg = f"Database error while checking discussion existence: {e}"
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error while checking discussion existence: {e}"
            self.logger.error(error_msg)
            raise DiscussionManagerError(error_msg)

    def _validate_discussion_input(self, personas: List[Persona], topic: str) -> None:
        """
        Validate input parameters for discussion start.

        Args:
            personas: List of personas to validate
            topic: Topic string to validate

        Raises:
            DiscussionManagerError: If validation fails
        """
        # Validate personas
        if not personas:
            raise DiscussionManagerError("議論参加ペルソナが指定されていません")

        if len(personas) < 2:
            raise DiscussionManagerError("議論には最低2つのペルソナが必要です")

        if len(personas) > 5:
            raise DiscussionManagerError("議論参加ペルソナは最大5つまでです")

        # Validate each persona
        for i, persona in enumerate(personas):
            if not persona:
                raise DiscussionManagerError(f"ペルソナ {i + 1} が無効です")

            if not persona.id or not persona.name:
                raise DiscussionManagerError(
                    f"ペルソナ {i + 1} のIDまたは名前が設定されていません"
                )

        # Check for duplicate personas
        persona_ids = [persona.id for persona in personas]
        if len(set(persona_ids)) != len(persona_ids):
            raise DiscussionManagerError("重複したペルソナが含まれています")

        # Validate topic
        if not topic or not topic.strip():
            raise DiscussionManagerError("議論トピックが空です")

        if len(topic.strip()) < 5:
            raise DiscussionManagerError(
                "議論トピックが短すぎます。5文字以上で入力してください"
            )

        if len(topic.strip()) > 200:
            raise DiscussionManagerError(
                "議論トピックが長すぎます。200文字以内で入力してください"
            )

    def _load_documents(
        self, document_ids: List[str]
    ) -> tuple[Optional[List[Dict[str, Any]]], Optional[List[Dict[str, Any]]]]:
        """
        Load document files and metadata from database.

        Args:
            document_ids: List of document IDs to load

        Returns:
            Tuple of (documents_data, documents_metadata):
                - documents_data: List of dicts with file_path and mime_type for AI service
                - documents_metadata: List of dicts with metadata for Discussion model

        Raises:
            DiscussionManagerError: If document loading fails
        """
        if not document_ids:
            return None, None

        documents_data = []
        documents_metadata = []
        total_size = 0
        max_total_size = 32 * 1024 * 1024  # 32MB (Bedrock API limit)

        for doc_id in document_ids:
            # Get file info from database
            file_info = self.database_service.get_uploaded_file_info(doc_id)
            if not file_info:
                raise DiscussionManagerError(f"ドキュメントが見つかりません: {doc_id}")

            # Validate file path exists
            file_path = file_info.get("file_path")
            if not file_path:
                raise DiscussionManagerError(
                    f"ドキュメントファイルパスが見つかりません: {doc_id}"
                )

            # S3パスの場合はファイル存在確認をスキップ（S3に保存されている前提）
            if not file_path.startswith("s3://"):
                # ローカルファイルの場合のみ存在確認
                if not Path(file_path).exists():
                    raise DiscussionManagerError(
                        f"ドキュメントファイルが存在しません: {doc_id}"
                    )

            # Check total size
            file_size = file_info.get("file_size", 0)
            total_size += file_size
            if total_size > max_total_size:
                raise DiscussionManagerError(
                    "ドキュメントの合計サイズが制限を超えています（最大32MB）"
                )

            # Add to documents_data for AI service
            documents_data.append(
                {
                    "file_path": file_path,
                    "mime_type": file_info.get("mime_type", "application/octet-stream"),
                }
            )

            # Get uploaded_at and convert to string if needed
            uploaded_at = file_info.get("uploaded_at", datetime.now())
            if isinstance(uploaded_at, datetime):
                uploaded_at = uploaded_at.isoformat()

            # Add to documents_metadata for Discussion model
            documents_metadata.append(
                {
                    "id": doc_id,
                    "filename": file_info.get("original_filename", "unknown"),
                    "file_path": file_path,
                    "file_size": file_size,
                    "mime_type": file_info.get("mime_type", "application/octet-stream"),
                    "uploaded_at": uploaded_at,
                }
            )

        self.logger.info(
            f"Loaded {len(documents_data)} documents (total size: {total_size} bytes)"
        )
        return documents_data, documents_metadata

    def _validate_discussion_results(
        self, discussion: Discussion, original_personas: List[Persona]
    ) -> None:
        """
        Validate discussion results after AI generation.

        Args:
            discussion: Generated discussion to validate
            original_personas: Original personas that participated

        Raises:
            DiscussionManagerError: If validation fails
        """
        if not discussion:
            raise DiscussionManagerError("生成された議論が無効です")

        if not discussion.messages:
            raise DiscussionManagerError("議論にメッセージが含まれていません")

        if len(discussion.messages) < 2:
            raise DiscussionManagerError("議論メッセージが少なすぎます")

        # Check that all personas have at least one message
        persona_message_count: dict[str, int] = {}
        for message in discussion.messages:
            persona_message_count[message.persona_id] = (
                persona_message_count.get(message.persona_id, 0) + 1
            )

        for persona in original_personas:
            if persona_message_count.get(persona.id, 0) == 0:
                self.logger.warning(
                    f"ペルソナ {persona.name} の発言が見つかりませんでした"
                )

        # Validate message content quality
        total_content_length = sum(len(msg.content) for msg in discussion.messages)
        if total_content_length < 100:
            raise DiscussionManagerError(
                "議論内容が短すぎます。より詳細な議論が必要です"
            )

    def _validate_discussion_for_save(self, discussion: Discussion) -> None:
        """
        Validate a discussion object before saving.

        Args:
            discussion: Discussion object to validate

        Raises:
            DiscussionManagerError: If validation fails
        """
        if not discussion:
            raise DiscussionManagerError("議論オブジェクトが無効です")

        if not discussion.id:
            raise DiscussionManagerError("議論IDが設定されていません")

        if not discussion.topic or not discussion.topic.strip():
            raise DiscussionManagerError("議論トピックが設定されていません")

        if not discussion.participants or len(discussion.participants) < 2:
            raise DiscussionManagerError("議論参加者が不足しています")

        if not discussion.created_at:
            raise DiscussionManagerError("議論作成日時が設定されていません")

        # Validate messages if present
        if discussion.messages:
            for i, message in enumerate(discussion.messages):
                if not message.persona_id or not message.content:
                    raise DiscussionManagerError(f"メッセージ {i + 1} が無効です")

        # Validate insights if present
        if discussion.insights:
            for i, insight in enumerate(discussion.insights):
                if not insight.category or not insight.description:
                    raise DiscussionManagerError(f"インサイト {i + 1} が無効です")

    def _parse_insights_from_structured_data(
        self, insight_data_list: List[Dict[str, Any]]
    ) -> List[Insight]:
        """
        Parse structured insight data into Insight objects.

        Args:
            insight_data_list: List of structured insight data dictionaries

        Returns:
            List[Insight]: Parsed Insight objects

        Raises:
            DiscussionManagerError: If parsing fails
        """
        insights = []

        for i, insight_data in enumerate(insight_data_list):
            try:
                # Validate required fields
                if not isinstance(insight_data, dict):
                    self.logger.warning(
                        f"Invalid insight data at index {i}: not a dictionary"
                    )
                    continue

                required_fields = ["category", "description", "confidence_score"]
                for field in required_fields:
                    if field not in insight_data:
                        raise ValueError(f"Missing required field: {field}")

                # Create insight object with AI-provided confidence score
                insight = Insight.create_new(
                    category=insight_data["category"],
                    description=insight_data["description"],
                    supporting_messages=[],  # Will be populated later if needed
                    confidence_score=float(insight_data["confidence_score"]),
                )

                insights.append(insight)

            except Exception as e:
                self.logger.warning(f"Failed to parse insight at index {i}: {e}")
                # Continue with other insights instead of failing completely
                continue

        return insights

    def _parse_insights_from_texts(self, insight_texts: List[str]) -> List[Insight]:
        """
        Parse insight texts into Insight objects.

        Args:
            insight_texts: List of insight text strings

        Returns:
            List[Insight]: Parsed Insight objects

        Raises:
            DiscussionManagerError: If parsing fails
        """
        insights = []

        for i, text in enumerate(insight_texts):
            if not text or not text.strip():
                self.logger.warning(f"Empty insight text at index {i}, skipping")
                continue

            try:
                # Parse category and description from text
                category, description = self._extract_category_and_description(
                    text.strip()
                )

                # Create insight object
                insight = Insight.create_new(
                    category=category,
                    description=description,
                    supporting_messages=[],  # Will be populated later if needed
                    confidence_score=0.8,  # Default confidence score
                )

                insights.append(insight)

            except Exception as e:
                self.logger.warning(f"Failed to parse insight at index {i}: {e}")
                # Continue with other insights instead of failing completely
                continue

        return insights

    def _extract_category_and_description(self, text: str) -> tuple[str, str]:
        """
        Extract category and description from insight text.

        Args:
            text: Insight text to parse

        Returns:
            tuple: (category, description)
        """
        # Look for category pattern: [カテゴリー] 内容
        if text.startswith("[") and "]" in text:
            end_bracket = text.index("]")
            category = text[1:end_bracket].strip()
            description = text[end_bracket + 1 :].strip()

            # Remove leading whitespace or dash
            if description.startswith("-") or description.startswith("–"):
                description = description[1:].strip()

            return category, description
        else:
            # If no category pattern found, use default category
            return "その他", text.strip()

    def _validate_generated_insights(self, insights: List[Insight]) -> None:
        """
        Validate generated insights.

        Args:
            insights: List of insights to validate

        Raises:
            DiscussionManagerError: If validation fails
        """
        if not insights:
            raise DiscussionManagerError("インサイトが生成されませんでした")

        if len(insights) < 3:
            self.logger.warning(
                f"生成されたインサイト数が少なすぎます: {len(insights)}"
            )
            # Warning only, don't fail

        for i, insight in enumerate(insights):
            if not insight:
                raise DiscussionManagerError(f"インサイト {i + 1} が無効です")

            if not insight.category or not insight.category.strip():
                raise DiscussionManagerError(
                    f"インサイト {i + 1} のカテゴリが設定されていません"
                )

            if not insight.description or not insight.description.strip():
                raise DiscussionManagerError(
                    f"インサイト {i + 1} の説明が設定されていません"
                )

            if len(insight.description.strip()) < 10:
                raise DiscussionManagerError(f"インサイト {i + 1} の説明が短すぎます")

    def _save_categories_to_config(
        self, discussion: Discussion, categories: List[InsightCategory]
    ) -> Discussion:
        """
        Save insight categories to discussion's agent_config.

        Args:
            discussion: Discussion object to update
            categories: List of insight categories to save

        Returns:
            Discussion: Updated discussion with categories in agent_config
        """
        if not categories:
            return discussion

        # Convert categories to dict format for storage
        categories_data = [cat.to_dict() for cat in categories]

        # Update or create agent_config
        agent_config = discussion.agent_config or {}
        agent_config["insight_categories"] = categories_data

        # Create new discussion instance with updated config
        return Discussion(
            id=discussion.id,
            topic=discussion.topic,
            participants=discussion.participants,
            messages=discussion.messages,
            insights=discussion.insights,
            created_at=discussion.created_at,
            mode=discussion.mode,
            agent_config=agent_config,
            documents=discussion.documents,
        )

    def _load_categories_from_config(
        self, discussion: Discussion
    ) -> Optional[List[InsightCategory]]:
        """
        Load insight categories from discussion's agent_config.

        Args:
            discussion: Discussion object to load from

        Returns:
            Optional[List[InsightCategory]]: List of categories if found, None otherwise
        """
        if not discussion.agent_config:
            return None

        categories_data = discussion.agent_config.get("insight_categories")
        if not categories_data:
            return None

        try:
            return [InsightCategory.from_dict(cat_data) for cat_data in categories_data]
        except Exception as e:
            self.logger.warning(f"Failed to load categories from config: {e}")
            return None
