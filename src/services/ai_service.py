"""
AI service
Provides AI features using Amazon Bedrock
"""

import json
import time
import logging
from typing import List, Dict, Any, Optional

try:
    import boto3
    from botocore.exceptions import ClientError, BotoCoreError
except ImportError:
    # Fallback for test environments or when boto3 is not installed
    boto3 = None
    ClientError = Exception
    BotoCoreError = Exception

from ..config import config
from ..models.errors import CodedError, ErrorCode
from ..models.persona import Persona
from ..models.message import Message
from ..models.insight_category import InsightCategory


class AIServiceError(CodedError):
    """Errors raised by the AI service."""

    pass


class BedrockConnectionError(AIServiceError):
    """Bedrock connection error."""

    pass


class BedrockAPIError(AIServiceError):
    """Bedrock API error."""

    pass


class AIService:
    """AI service using Amazon Bedrock."""

    def __init__(self, bedrock_client: Any = None) -> None:
        """
        Initialize the AI service

        Args:
            bedrock_client: Bedrock client (for tests)
        """
        self.logger = logging.getLogger(__name__)
        self.model_id = config.BEDROCK_MODEL_ID
        self.max_tokens = config.MAX_TOKENS

        # Retry settings
        self.max_retries = 3
        self.base_delay = 1.0
        self.max_delay = 60.0

        if bedrock_client:
            self.bedrock_client = bedrock_client
        else:
            self.bedrock_client = self._create_bedrock_client()

    def _create_bedrock_client(self) -> Any:
        """Create the Bedrock client."""
        if not boto3:
            raise BedrockConnectionError(
                "boto3 package is not installed",
                code=ErrorCode.AI_BEDROCK_UNAVAILABLE,
            )

        try:
            from botocore.config import Config as BotoConfig

            # Get AWS credentials
            credentials = config.get_aws_credentials()

            # Drop None values
            filtered_credentials = {
                k: v for k, v in credentials.items() if v is not None
            }

            # Add a timeout (persona generation is slow, so it is long)
            boto_config = BotoConfig(
                connect_timeout=30,
                read_timeout=300,  # 5 minutes (enough for persona generation and discussions)
                retries={"max_attempts": 0},  # Retries are controlled by our own code
            )

            # Create the Bedrock Runtime client
            client = boto3.client(
                "bedrock-runtime", config=boto_config, **filtered_credentials
            )

            self.logger.info(f"Bedrock client created (region: {config.AWS_REGION})")
            return client

        except Exception as e:
            self.logger.error(f"Failed to create Bedrock client: {e}")
            raise BedrockConnectionError(
                f"bedrock client creation failed ({type(e).__name__})",
                code=ErrorCode.AI_BEDROCK_CONNECTION_FAILED,
            ) from e

    def _retry_with_backoff(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """
        Run with exponential-backoff retries
        """
        last_exception = None

        for attempt in range(self.max_retries):
            try:
                return func(*args, **kwargs)
            except (ClientError, BotoCoreError) as e:
                last_exception = e

                if attempt == self.max_retries - 1:
                    break

                if not self._is_retryable_error(e):
                    break

                delay = min(self.base_delay * (2**attempt), self.max_delay)
                self.logger.warning(
                    f"API error occurred. Retrying in {delay}s (attempt {attempt + 1}/{self.max_retries}): {e}"
                )
                time.sleep(delay)

        error_msg = f"max retries ({self.max_retries}) reached: {last_exception}"
        self.logger.error(error_msg)
        raise BedrockAPIError(
            f"max retries ({self.max_retries}) exhausted",
            code=ErrorCode.AI_BEDROCK_API_FAILED,
        ) from last_exception

    def _is_retryable_error(self, error: Exception) -> bool:
        """Decide whether the error is retryable."""
        if isinstance(error, ClientError):
            error_code = error.response.get("Error", {}).get("Code", "")
            retryable_codes = [
                "ThrottlingException",
                "ServiceUnavailableException",
                "InternalServerException",
                "TooManyRequestsException",
            ]
            return error_code in retryable_codes

        return isinstance(error, BotoCoreError)

    @staticmethod
    def _extract_first_converse_text_block(content_blocks: List[Dict[str, Any]]) -> str:
        """Extract the first text block from the Converse API content array.

        A Converse API text block is {"text": "..."} with no type field, so
        it is identified by excluding thinking blocks {"reasoningContent": {...}}.

        """
        for block in content_blocks:
            if (
                isinstance(block, dict)
                and "reasoningContent" not in block
                and "text" in block
            ):
                return str(block["text"])
        raise BedrockAPIError(
            "no text block in response content",
            code=ErrorCode.AI_BEDROCK_API_FAILED,
        )

    def invoke_model(self, prompt: str, max_tokens: Optional[int] = None) -> str:
        """Call a Bedrock model (via the Converse API)

        Args:
            prompt: Prompt string
            max_tokens: Maximum tokens (the default is used if None)
        """
        messages = [{"role": "user", "content": [{"text": prompt}]}]
        return self._invoke_converse_api(messages, max_tokens=max_tokens)

    def _invoke_converse_api(
        self,
        messages: List[Dict[str, Any]],
        system_prompts: Optional[List[Dict[str, str]]] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """
        Call the Bedrock Converse API (multimodal supported)

        Args:
            messages: Message list (content-array format)
            system_prompts: System prompts (optional)

        Returns:
            str: Response text from the model

        Raises:
            BedrockAPIError: API call error
        """
        try:
            request_params = {
                "modelId": self.model_id,
                "messages": messages,
                "inferenceConfig": {
                    "maxTokens": max_tokens or self.max_tokens,
                },
            }

            if system_prompts:
                request_params["system"] = system_prompts

            response = self.bedrock_client.converse(**request_params)

            # Extract the response text from the response
            if "output" in response and "message" in response["output"]:
                message = response["output"]["message"]
                if "content" in message and len(message["content"]) > 0:
                    return self._extract_first_converse_text_block(message["content"])

            raise BedrockAPIError(
                "converse api response is empty",
                code=ErrorCode.AI_BEDROCK_API_FAILED,
            )

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))
            raise BedrockAPIError(
                f"converse api call failed ({error_code}): {error_msg}",
                code=ErrorCode.AI_BEDROCK_API_FAILED,
            ) from e
        except BotoCoreError:
            # Re-raise connection errors etc. as is so _retry_with_backoff retries them
            raise
        except Exception as e:
            if isinstance(e, BedrockAPIError):
                raise
            raise BedrockAPIError(
                f"converse api call failed unexpectedly ({type(e).__name__})",
                code=ErrorCode.AI_BEDROCK_API_FAILED,
            ) from e

    def _prepare_document_content(
        self, documents: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Convert documents to the content format for the Converse API

        Args:
            documents: List of document metadata
                Each dict has the following keys:
                - file_path: File path (local or s3://)
                - mime_type: MIME type
                - original_filename: Original file name

        Returns:
            List[Dict[str, Any]]: Content list for the Converse API

        Raises:
            AIServiceError: Document processing error
        """
        content_list = []

        for doc in documents:
            try:
                file_path = doc.get("file_path", "")
                mime_type = doc.get("mime_type", "")
                filename = doc.get("original_filename", "document")

                # Read the file (S3 or local)
                if file_path.startswith("s3://"):
                    # Read from S3
                    from .service_factory import service_factory

                    s3_service = service_factory.get_s3_service()

                    # Pass the whole S3 path
                    file_bytes = s3_service.download_file(file_path)
                else:
                    # Read from a local file
                    with open(file_path, "rb") as f:
                        file_bytes = f.read()

                # Decide the content format by MIME type
                if mime_type.startswith("image/"):
                    # For images (pass the raw bytes)
                    image_format = mime_type.split("/")[-1]  # 'png', 'jpeg'
                    content_list.append(
                        {
                            "image": {
                                "format": image_format,
                                "source": {"bytes": file_bytes},
                            }
                        }
                    )
                elif mime_type == "application/pdf":
                    # For PDFs (pass the raw bytes)
                    content_list.append(
                        {
                            "document": {
                                "name": filename,
                                "format": "pdf",
                                "source": {"bytes": file_bytes},
                            }
                        }
                    )
                else:
                    self.logger.warning(f"Unsupported MIME type: {mime_type}")

            except FileNotFoundError as e:
                self.logger.error(f"File not found: {file_path}")
                raise AIServiceError(
                    "document file not found",
                    code=ErrorCode.AI_OPERATION_FAILED,
                ) from e
            except Exception as e:
                self.logger.error(f"Document processing error: {e}")
                raise AIServiceError(
                    f"document processing failed ({type(e).__name__})",
                    code=ErrorCode.AI_OPERATION_FAILED,
                ) from e

        return content_list

    def _facilitate_discussion_with_documents(
        self, personas: List[Persona], topic: str, documents: List[Dict[str, Any]]
    ) -> List[Message]:
        """
        Run a discussion with documents (using the Converse API)

        Args:
            personas: List of personas in the discussion
            topic: Discussion topic
            documents: List of attached documents

        Returns:
            List[Message]: List of discussion messages

        Raises:
            AIServiceError: Discussion error
        """
        # Convert the documents to content format
        document_contents = self._prepare_document_content(documents)

        # Build the discussion prompt
        discussion_prompt = self._create_discussion_prompt(personas, topic)

        # Build the message (text + documents)
        content = [{"text": discussion_prompt}] + document_contents

        messages = [{"role": "user", "content": content}]

        # Call the Converse API
        response = self._retry_with_backoff(self._invoke_converse_api, messages)

        # Parse the response
        parsed_messages = self._parse_discussion_response(response, personas)

        return parsed_messages

    def _extract_json_from_response(
        self, response: str, prefer_array: bool = False
    ) -> str:
        """
        Extract the JSON part from a response

        Args:
            response: Response string from the AI

        Returns:
            str: The extracted JSON string

        Raises:
            AIServiceError: If no JSON is found
        """
        # Clean up the response
        response = response.strip()

        if prefer_array:
            # Prefer array form (for insight extraction)
            array_start = response.find("[")
            if array_start != -1:
                # Find the matching closing bracket
                bracket_count = 0
                end_idx = -1

                for i in range(array_start, len(response)):
                    if response[i] == "[":
                        bracket_count += 1
                    elif response[i] == "]":
                        bracket_count -= 1
                        if bracket_count == 0:
                            end_idx = i
                            break

                if end_idx != -1:
                    json_candidate = response[array_start : end_idx + 1]
                    # Test whether the extracted JSON is valid
                    try:
                        json.loads(json_candidate)
                        return json_candidate
                    except json.JSONDecodeError:
                        self.logger.warning(
                            f"Array JSON candidate is invalid: {json_candidate[:100]}..."
                        )

            # If no array is found, look for an object
            start_idx = response.find("{")
            if start_idx != -1:
                # Find the matching closing bracket
                brace_count = 0
                end_idx = -1

                for i in range(start_idx, len(response)):
                    if response[i] == "{":
                        brace_count += 1
                    elif response[i] == "}":
                        brace_count -= 1
                        if brace_count == 0:
                            end_idx = i
                            break

                if end_idx != -1:
                    json_candidate = response[start_idx : end_idx + 1]
                    # Test whether the extracted JSON is valid
                    try:
                        json.loads(json_candidate)
                        return json_candidate
                    except json.JSONDecodeError:
                        self.logger.warning(
                            f"Object JSON candidate is invalid: {json_candidate[:100]}..."
                        )
        else:
            # Prefer object form (for persona generation)
            start_idx = response.find("{")
            if start_idx != -1:
                # Find the matching closing bracket
                brace_count = 0
                end_idx = -1

                for i in range(start_idx, len(response)):
                    if response[i] == "{":
                        brace_count += 1
                    elif response[i] == "}":
                        brace_count -= 1
                        if brace_count == 0:
                            end_idx = i
                            break

                if end_idx != -1:
                    json_candidate = response[start_idx : end_idx + 1]
                    # Test whether the extracted JSON is valid
                    try:
                        json.loads(json_candidate)
                        return json_candidate
                    except json.JSONDecodeError:
                        self.logger.warning(
                            f"Object JSON candidate is invalid: {json_candidate[:100]}..."
                        )

            # If no object is found, look for an array
            array_start = response.find("[")
            if array_start != -1:
                # Find the matching closing bracket
                bracket_count = 0
                end_idx = -1

                for i in range(array_start, len(response)):
                    if response[i] == "[":
                        bracket_count += 1
                    elif response[i] == "]":
                        bracket_count -= 1
                        if bracket_count == 0:
                            end_idx = i
                            break

                if end_idx != -1:
                    json_candidate = response[array_start : end_idx + 1]
                    # Test whether the extracted JSON is valid
                    try:
                        json.loads(json_candidate)
                        return json_candidate
                    except json.JSONDecodeError:
                        self.logger.warning(
                            f"Array JSON candidate is invalid: {json_candidate[:100]}..."
                        )

        # Look for a JSON block (```json ... ```)
        json_block_start = response.find("```json")
        if json_block_start != -1:
            json_content_start = response.find("\n", json_block_start) + 1
            json_block_end = response.find("```", json_content_start)
            if json_block_end != -1:
                json_candidate = response[json_content_start:json_block_end].strip()
                try:
                    json.loads(json_candidate)
                    return json_candidate
                except json.JSONDecodeError:
                    self.logger.warning(
                        f"JSON block candidate is invalid: {json_candidate[:100]}..."
                    )

        # Last resort: check whether the whole response is JSON
        try:
            json.loads(response)
            return response
        except json.JSONDecodeError:
            pass

        # If everything failed
        self.logger.error(f"JSON extraction failed - full response: {response}")
        raise AIServiceError(
            "could not extract valid JSON from response",
            code=ErrorCode.AI_OPERATION_FAILED,
        )

    def facilitate_discussion(
        self,
        personas: List[Persona],
        topic: str,
        documents: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Message]:
        """
        Run a discussion between personas

        Args:
            personas: List of personas in the discussion
            topic: Discussion topic
            documents: List of attached documents (optional)

        Returns:
            List[Message]: List of discussion messages

        Raises:
            AIServiceError: Discussion error
        """
        self.logger.info(
            f"Starting discussion (participants: {len(personas)}, topic: {topic[:50]}..., documents: {len(documents) if documents else 0})"
        )

        # Use the Converse API when there are documents
        if documents:
            try:
                messages = self._facilitate_discussion_with_documents(
                    personas, topic, documents
                )
                self.logger.info(
                    f"Discussion completed (message count: {len(messages)})"
                )
                return messages

            except AIServiceError:
                raise
            except Exception as e:
                error_msg = f"Error during discussion with documents: {e}"
                self.logger.error(error_msg)
                raise AIServiceError(
                    f"discussion with documents failed ({type(e).__name__})",
                    code=ErrorCode.AI_OPERATION_FAILED,
                ) from e

        # Without documents, use the legacy method
        prompt = self._create_discussion_prompt(personas, topic)

        try:
            response = self._retry_with_backoff(self.invoke_model, prompt)
            messages = self._parse_discussion_response(response, personas)

            self.logger.info(f"Discussion completed (message count: {len(messages)})")
            return messages

        except AIServiceError:
            # Re-raise AIServiceError
            raise
        except Exception as e:
            error_msg = f"Error during discussion facilitation: {e}"
            self.logger.error(error_msg)
            raise AIServiceError(
                f"discussion facilitation failed ({type(e).__name__})",
                code=ErrorCode.AI_OPERATION_FAILED,
            ) from e

    def facilitate_discussion_streaming(
        self,
        personas: List[Persona],
        topic: str,
        documents: Optional[List[Dict[str, Any]]] = None,
    ) -> Any:
        """
        Run a discussion between personas (streaming version)
        Yields each statement

        Args:
            personas: List of personas in the discussion
            topic: Discussion topic
            documents: Optional document list (includes file_path, mime_type)

        Yields:
            Message: Each statement message

        Raises:
            AIServiceError: Discussion error
        """
        self.logger.info(
            f"Starting streaming discussion (participants: {len(personas)})"
        )

        prompt = self._create_discussion_prompt(personas, topic)
        persona_map = {persona.name: persona.id for persona in personas}

        try:
            # Build the message content (add attachments when there are documents)
            message_content: List[Dict[str, Any]] = [{"text": prompt}]
            if documents:
                message_content += self._prepare_document_content(documents)

            def _call_converse_stream() -> Any:
                return self.bedrock_client.converse_stream(
                    modelId=self.model_id,
                    messages=[{"role": "user", "content": message_content}],
                    inferenceConfig={
                        "maxTokens": self.max_tokens,
                    },
                )

            # Retry with exponential backoff against transient connection errors
            response = self._retry_with_backoff(_call_converse_stream)

            # Process the streaming response
            accumulated_text = ""
            yielded_messages = set()

            for event in response.get("stream", []):
                if "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"].get("delta", {})
                    if "reasoningContent" not in delta and "text" in delta:
                        accumulated_text += delta["text"]

                        # Detect completed messages and yield them
                        lines = accumulated_text.split("\n")
                        for line in lines[:-1]:  # The last line may be incomplete
                            line = line.strip()
                            if line.startswith("[") and "]:" in line:
                                try:
                                    end_bracket = line.index("]:")
                                    persona_name = line[1:end_bracket]
                                    content = line[end_bracket + 2 :].strip()

                                    message_key = f"{persona_name}:{content[:50]}"
                                    if (
                                        persona_name in persona_map
                                        and content
                                        and message_key not in yielded_messages
                                    ):
                                        message = Message.create_new(
                                            persona_id=persona_map[persona_name],
                                            persona_name=persona_name,
                                            content=content,
                                        )
                                        yielded_messages.add(message_key)
                                        yield message
                                except (ValueError, IndexError):
                                    continue

            self.logger.info("Streaming discussion completed")

        except Exception as e:
            error_msg = f"Error during streaming discussion: {e}"
            self.logger.error(error_msg)
            raise AIServiceError(
                f"streaming discussion failed ({type(e).__name__})",
                code=ErrorCode.AI_OPERATION_FAILED,
            ) from e

    def extract_insights(
        self,
        discussion_messages: List[Message],
        categories: Optional[List[InsightCategory]] = None,
        topic: str = "",
        participant_profiles: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Extract insights from discussion messages

        Args:
            discussion_messages: List of discussion messages
            categories: List of insight categories (defaults are used if None)
            topic: Discussion topic
            participant_profiles: One prompt line per participant (who they are)

        Returns:
            List[Dict[str, Any]]: Structured data of the extracted insights
                Each dict has the following keys:
                - category: str (category)
                - description: str (description)
                - confidence_score: float (confidence score 0.0-1.0)
                - supporting_messages: list[str] (quoted statements)

        Raises:
            AIServiceError: Insight extraction error
        """
        # Use the defaults when no categories are given
        if categories is None:
            categories = config.get_default_insight_categories()

        total_chars = sum(len(msg.content) for msg in discussion_messages)
        self.logger.info(
            f"Starting insight extraction (message count: {len(discussion_messages)}, total chars: {total_chars}, category count: {len(categories)})"
        )

        prompt = self._create_insight_extraction_prompt(
            discussion_messages, categories, topic, participant_profiles
        )

        try:
            response = self._retry_with_backoff(
                self.invoke_model, prompt, max_tokens=config.INSIGHT_MAX_TOKENS
            )
            self.logger.debug(f"Insight extraction AI response: {response[:500]}...")
            insights = self._parse_structured_insights_response(response, categories)

            self.logger.info(
                f"Insight extraction completed (insight count: {len(insights)})"
            )
            return insights

        except AIServiceError:
            raise
        except Exception as e:
            error_msg = f"Error during insight extraction: {e}"
            self.logger.error(error_msg)
            raise AIServiceError(
                f"insight extraction failed ({type(e).__name__})",
                code=ErrorCode.AI_OPERATION_FAILED,
            ) from e

    def _create_discussion_prompt(self, personas: List[Persona], topic: str) -> str:
        """Build the prompt for running a discussion."""
        from ..prompts.discussion_interview_prompts import build_discussion_prompt

        return build_discussion_prompt(personas, topic)

    def _create_insight_extraction_prompt(
        self,
        messages: List[Message],
        categories: Optional[List[InsightCategory]],
        topic: str = "",
        participant_profiles: Optional[List[str]] = None,
    ) -> str:
        """Build the prompt for insight extraction."""
        from ..prompts.insight_prompts import build_insight_extraction_prompt

        return build_insight_extraction_prompt(
            messages, categories, topic, participant_profiles=participant_profiles
        )

    def _parse_discussion_response(
        self, response: str, personas: List[Persona]
    ) -> List[Message]:
        """Parse the discussion response into a message list."""
        messages = []
        lines = response.strip().split("\n")

        # Build the persona name mapping
        persona_map = {persona.name: persona.id for persona in personas}

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Parse the "[persona name]: statement" format
            if line.startswith("[") and "]:" in line:
                try:
                    end_bracket = line.index("]:")
                    persona_name = line[1:end_bracket]
                    content = line[end_bracket + 2 :].strip()

                    if persona_name in persona_map and content:
                        message = Message.create_new(
                            persona_id=persona_map[persona_name],
                            persona_name=persona_name,
                            content=content,
                        )
                        messages.append(message)
                except (ValueError, IndexError):
                    # Skip lines that fail to parse
                    self.logger.warning(
                        f"Skipping line that failed to parse in discussion response: {line}"
                    )
                    continue

        # Check the minimum message count
        if len(messages) < 2:
            self.logger.warning(f"Parsed message count is too low: {len(messages)}")
            raise AIServiceError(
                f"parsed message count {len(messages)} below minimum 2",
                code=ErrorCode.AI_OPERATION_FAILED,
            )

        return messages

    def _parse_structured_insights_response(
        self, response: str, categories: List[InsightCategory]
    ) -> List[Dict[str, Any]]:
        """
        Parse a structured insight response

        Args:
            response: Response string from the AI (JSON format)
            categories: List of valid insight categories

        Returns:
            List[Dict[str, Any]]: Structured data of the parsed insights

        Raises:
            AIServiceError: Parse error
        """
        try:
            # Extract the JSON part (array form preferred for insight extraction)
            json_str = self._extract_json_from_response(response, prefer_array=True)

            # Parse the JSON
            insights_data = json.loads(json_str)

            if not isinstance(insights_data, list):
                raise AIServiceError(
                    "insights data is not a list",
                    code=ErrorCode.AI_OPERATION_FAILED,
                )

            # Validate and normalize each insight
            validated_insights = []
            for i, insight in enumerate(insights_data):
                try:
                    validated_insight = self._validate_and_normalize_insight(
                        insight, i, categories
                    )
                    validated_insights.append(validated_insight)
                except Exception as e:
                    self.logger.warning(f"Failed to validate insight {i + 1}: {e}")
                    continue

            if not validated_insights:
                raise AIServiceError(
                    "no valid insights found",
                    code=ErrorCode.AI_OPERATION_FAILED,
                )

            return validated_insights

        except json.JSONDecodeError as e:
            self.logger.warning(
                f"JSON parsing failed, falling back to text parsing: {e}"
            )
            # Fallback: use the legacy string parsing
            return self._fallback_to_text_parsing(response)
        except Exception as e:
            self.logger.error(f"Structured insight parsing error: {e}")
            # Fallback: use the legacy string parsing
            return self._fallback_to_text_parsing(response)

    def _validate_and_normalize_insight(
        self, insight: Dict[str, Any], index: int, categories: List[InsightCategory]
    ) -> Dict[str, Any]:
        """Validate required fields of insights extracted from an AI response and convert types.

        Business rules (category normalization, confidence clamp, deduplication) are
        applied in the Manager layer (shared/insight_utils).
        """
        if not isinstance(insight, dict):
            raise AIServiceError(
                f"insight at index {index} is not a dict",
                code=ErrorCode.AI_OPERATION_FAILED,
            )

        required_fields = ["category", "description", "confidence_score"]
        for field in required_fields:
            if field not in insight:
                raise AIServiceError(
                    f"insight at index {index} missing required field {field!r}",
                    code=ErrorCode.AI_OPERATION_FAILED,
                )

        description = str(insight["description"]).strip()
        if not description:
            raise AIServiceError(
                f"insight at index {index} has empty description",
                code=ErrorCode.AI_OPERATION_FAILED,
            )

        try:
            confidence_score = float(insight["confidence_score"])
        except (ValueError, TypeError):
            confidence_score = 0.5

        raw_supporting = insight.get("supporting_messages")
        supporting_messages = (
            [
                str(quote).strip()
                for quote in raw_supporting
                if isinstance(quote, str) and quote.strip()
            ]
            if isinstance(raw_supporting, list)
            else []
        )

        return {
            "category": str(insight["category"]).strip(),
            "description": description,
            "confidence_score": confidence_score,
            "supporting_messages": supporting_messages,
        }

    def _fallback_to_text_parsing(self, response: str) -> List[Dict[str, Any]]:
        """
        Fallback: extract insights using legacy text parsing

        Args:
            response: Response string from the AI

        Returns:
            List[Dict[str, Any]]: Structured data of the parsed insights
        """
        self.logger.info("Fallback: extracting insights using text parsing")

        # Use the legacy method
        text_insights = self._parse_insights_response(response)

        # Convert text insights into structured data
        structured_insights = []
        for insight_text in text_insights:
            category, description = self._extract_category_and_description(insight_text)

            # Set a default confidence score (adjusted by discussion length)
            default_confidence = 0.6  # Medium confidence

            structured_insights.append(
                {
                    "category": category,
                    "description": description,
                    "confidence_score": default_confidence,
                }
            )

        self.logger.info(
            f"Fallback processing completed: extracted {len(structured_insights)} insights"
        )
        return structured_insights

    def _extract_category_and_description(self, text: str) -> tuple[str, str]:
        """
        Extract category and description from insight text.

        Args:
            text: Insight text to parse

        Returns:
            tuple: (category, description)
        """
        # Look for category pattern: [category] content
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

    def _parse_insights_response(self, response: str) -> List[str]:
        """Parse an insight response.

        Supported formats:
        - Bullets: `- [category] content` or `- content`
        - Numbered: `1. [category] content` or `1. content`
        - Bold category (Claude style): `**[category]** content` or `**category**: content`
        - Category only: `[category] content`
        - Heading style: content after `## category`
        """
        insights = []
        lines = response.strip().split("\n")
        current_category = None

        for line in lines:
            line = line.strip()
            if not line:
                continue

            insight = None

            # Detect heading-style categories (lines starting with ## or ###)
            if line.startswith("#"):
                # Extract and store the category name
                current_category = line.lstrip("#").strip()
                continue

            # 1. Strip the bullet marker
            if line.startswith(("- ", "• ", "* ", "・")):
                content = line[2:].strip()
                if content:
                    insight = content
            # 2. Numbered list
            elif line.startswith(
                tuple(f"{i}." for i in range(1, 10))
            ) or line.startswith(tuple(f"{i})" for i in range(1, 10))):
                parts = line.split(".", 1) if "." in line[:3] else line.split(")", 1)
                if len(parts) > 1:
                    insight = parts[1].strip()
            # 3. Claude style: starts with **[category]**
            elif line.startswith("**[") and "]**" in line:
                insight = line.replace("**[", "[").replace("]**", "]")
            # 4. Claude style: **category**: content
            elif line.startswith("**") and "**:" in line:
                # **category**: content -> [category] content
                parts = line.split("**:", 1)
                if len(parts) == 2:
                    category = parts[0].replace("**", "").strip()
                    content = parts[1].strip()
                    if content:
                        insight = f"[{category}] {content}"
            # 5. Starts with [category] (no bullet marker)
            elif line.startswith("[") and "]" in line:
                insight = line
            # 6. Line starting with bold (no category)
            elif line.startswith("**") and line.endswith("**"):
                # Treat as a heading
                current_category = line.strip("*").strip()
                continue
            # 7. Plain text line (if long enough)
            elif len(line) > 20 and not line.startswith(("#", "---", "===", "```")):
                # Attach the category if there is one
                if current_category:
                    insight = f"[{current_category}] {line}"
                else:
                    insight = line

            if insight and len(insight) > 10:
                insights.append(insight)

        return insights

    def generate_standard_report_streaming(
        self,
        system_prompt: str,
        converse_messages: List[Dict[str, Any]],
    ) -> Any:
        """Standard report streaming generation using Bedrock converse_stream.

        Template routing and context building are the caller's job (ReportManager).
        This method only makes the Bedrock API call.

        Args:
            system_prompt: System prompt
            converse_messages: Message list in Converse API format

        Yields:
            str: Text chunk
        """

        def _call_stream() -> Any:
            kwargs: Dict[str, Any] = {
                "modelId": self.model_id,
                "messages": converse_messages,
                "inferenceConfig": {
                    "maxTokens": 12000,
                },
            }
            if system_prompt:
                kwargs["system"] = [{"text": system_prompt}]
            return self.bedrock_client.converse_stream(**kwargs)

        response = self._retry_with_backoff(_call_stream)

        for event in response.get("stream", []):
            if "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                if "reasoningContent" not in delta and "text" in delta:
                    yield delta["text"]

    # =========================================================================
    # AI survey generation
    # =========================================================================

    _SURVEY_CHAT_SYSTEM_PROMPT = (
        "あなたはユーザー調査・アンケート設計の専門家として、ユーザーがアンケートテンプレートを"
        "作成するのを支援するアシスタントです。\n\n"
        "【対話方針】\n"
        "- 調査目的・想定ターゲット・聞きたい観点を不足なくヒアリングする。\n"
        "- 不明点は一度に1〜2問程度の簡潔な質問で尋ねる。\n"
        "- 既に十分情報が集まったと判断したら、『ドラフトを生成する準備ができました。右のパネルの「ドラフト生成」ボタンを押してください。』と案内する。\n"
        "- 回答は日本語で、親しみやすく簡潔に。Markdown記号は控えめに。\n"
        "- アンケート項目の具体的なJSONは出力せず、あくまで対話でヒアリングに徹する。"
    )

    _SURVEY_DRAFT_SYSTEM_PROMPT = (
        "あなたはユーザー調査・アンケート設計の専門家です。これまでのユーザーとのヒアリング会話を踏まえ、"
        "調査目的に沿った適切なアンケート設問のドラフトを生成してください。\n\n"
        "【要件】\n"
        "- 設問数は3〜8問の範囲で、調査内容に応じて過不足ないよう判断する。\n"
        "- 設問タイプは以下3種類のみ使用:\n"
        "  - multiple_choice: 選択式。options配列に2つ以上の選択肢を入れる。複数回答可なら allow_multiple=true。複数回答数に上限を設ける場合 max_selections を 1 以上に、無制限なら 0。\n"
        "  - free_text: 自由記述。options は空配列。\n"
        "  - scale_rating: 1〜5のスケール評価。options は空配列。\n"
        "- 設問文は簡潔で回答者が一意に解釈できる表現にする。\n"
        "- 必要に応じて選択式・自由記述・スケール評価をバランスよく組み合わせる。\n"
        "- template_name はアンケート内容を端的に表す30文字以内の日本語の名称にする。\n\n"
        "【出力形式】\n"
        "以下の JSON のみを出力してください。前置き・後書き・Markdownコードブロックは一切不要です。\n\n"
        "{\n"
        '  "template_name": "アンケートテンプレート名",\n'
        '  "summary": "生成した設問の狙いを1〜2行で説明",\n'
        '  "questions": [\n'
        "    {\n"
        '      "question_type": "multiple_choice",\n'
        '      "text": "...",\n'
        '      "options": ["選択肢1", "選択肢2"],\n'
        '      "allow_multiple": false,\n'
        '      "max_selections": 0\n'
        "    }\n"
        "  ]\n"
        "}"
    )

    @staticmethod
    def _to_converse_messages(messages: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        """Convert [{role, content}] into a message array for the Converse API."""
        converted: List[Dict[str, Any]] = []
        for m in messages:
            role = m.get("role", "user")
            content = (m.get("content") or "").strip()
            if not content or role not in ("user", "assistant"):
                continue
            converted.append({"role": role, "content": [{"text": content}]})
        return converted

    def chat_for_survey(
        self, messages: List[Dict[str, str]], system_prompt: str = ""
    ) -> str:
        """Multi-turn conversation for survey hearing.

        Args:
            messages: Conversation history [{"role": "user"|"assistant", "content": "..."}, ...]
                     (the last message is expected to be a user turn)
            system_prompt: System prompt

        Returns:
            The assistant's reply text
        """
        if not messages:
            raise AIServiceError(
                "conversation history is empty",
                code=ErrorCode.SURVEY_AI_CONVERSATION_INVALID,
            )
        if messages[-1].get("role") != "user":
            raise AIServiceError(
                "last message must have role 'user'",
                code=ErrorCode.SURVEY_AI_CONVERSATION_INVALID,
            )

        converse_messages = self._to_converse_messages(messages)
        if not converse_messages:
            raise AIServiceError(
                "no valid messages after conversion",
                code=ErrorCode.SURVEY_AI_CONVERSATION_INVALID,
            )

        prompt_text = system_prompt or self._SURVEY_CHAT_SYSTEM_PROMPT
        try:
            return str(
                self._retry_with_backoff(
                    self._invoke_converse_api,
                    converse_messages,
                    system_prompts=[{"text": prompt_text}],
                    max_tokens=1024,
                )
            )
        except AIServiceError:
            raise
        except Exception as e:
            raise AIServiceError(
                f"survey chat failed ({type(e).__name__})",
                code=ErrorCode.AI_OPERATION_FAILED,
            ) from e

    def generate_survey_questions_draft(
        self, messages: List[Dict[str, str]], system_prompt: str = ""
    ) -> Dict[str, Any]:
        """Generate a question draft from the conversation history and return a JSON dict.

        Args:
            messages: Conversation history
            system_prompt: System prompt

        Returns:
            {"summary": str, "questions": [ {question_type, text, options, allow_multiple, max_selections}, ... ]}
        """
        if not messages:
            raise AIServiceError(
                "conversation history is empty",
                code=ErrorCode.SURVEY_AI_CONVERSATION_INVALID,
            )

        # Append the "draft generation instruction" after the conversation history
        converse_messages = self._to_converse_messages(messages)
        converse_messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "text": (
                            "これまでのヒアリング内容に基づいて、アンケートの設問ドラフトを"
                            "指定のJSONスキーマに厳密に従って生成してください。"
                        )
                    }
                ],
            }
        )

        prompt_text = system_prompt or self._SURVEY_DRAFT_SYSTEM_PROMPT
        try:
            response = self._retry_with_backoff(
                self._invoke_converse_api,
                converse_messages,
                system_prompts=[{"text": prompt_text}],
                max_tokens=4096,
            )
        except AIServiceError:
            raise
        except Exception as e:
            raise AIServiceError(
                f"question draft generation failed ({type(e).__name__})",
                code=ErrorCode.AI_OPERATION_FAILED,
            ) from e

        try:
            json_str = self._extract_json_from_response(response, prefer_array=False)
            data = json.loads(json_str)
        except (AIServiceError, json.JSONDecodeError) as e:
            self.logger.error(
                f"Draft JSON parsing failed: {e} / response={response[:500]}"
            )
            raise AIServiceError(
                "question draft JSON parsing failed",
                code=ErrorCode.AI_OPERATION_FAILED,
            ) from e

        if not isinstance(data, dict) or "questions" not in data:
            raise AIServiceError(
                "question draft JSON has no 'questions' field",
                code=ErrorCode.SURVEY_AI_NO_QUESTIONS,
            )
        if not isinstance(data["questions"], list) or not data["questions"]:
            raise AIServiceError(
                "question draft 'questions' field is empty or not a list",
                code=ErrorCode.SURVEY_AI_NO_QUESTIONS,
            )

        data["summary"] = str(data.get("summary", "") or "").strip()
        data["template_name"] = str(data.get("template_name", "") or "").strip()[:50]
        return data
