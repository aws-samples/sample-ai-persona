"""
AI サービス
Amazon Bedrock を使用した AI 機能を提供する
"""

import json
import time
import logging
from typing import List, Dict, Any, Optional
from dataclasses import asdict

try:
    import boto3
    from botocore.exceptions import ClientError, BotoCoreError
except ImportError:
    # テスト環境やboto3がインストールされていない場合のフォールバック
    boto3 = None
    ClientError = Exception
    BotoCoreError = Exception

from ..config import config
from ..models.persona import Persona
from ..models.message import Message
from ..models.insight_category import InsightCategory


class AIServiceError(Exception):
    """AI サービス関連のエラー"""

    pass


class BedrockConnectionError(AIServiceError):
    """Bedrock 接続エラー"""

    pass


class BedrockAPIError(AIServiceError):
    """Bedrock API エラー"""

    pass


class AIService:
    """Amazon Bedrock を使用した AI サービス"""

    def __init__(self, bedrock_client: Any = None) -> None:
        """
        AI サービスの初期化

        Args:
            bedrock_client: Bedrock クライアント（テスト用）
        """
        self.logger = logging.getLogger(__name__)
        self.model_id = config.BEDROCK_MODEL_ID
        self.max_tokens = config.MAX_TOKENS
        self.temperature = config.TEMPERATURE

        # リトライ設定
        self.max_retries = 3
        self.base_delay = 1.0
        self.max_delay = 60.0

        if bedrock_client:
            self.bedrock_client = bedrock_client
        else:
            self.bedrock_client = self._create_bedrock_client()

    def _create_bedrock_client(self) -> Any:
        """Bedrock クライアントを作成"""
        if not boto3:
            raise BedrockConnectionError("boto3 がインストールされていません")

        try:
            from botocore.config import Config as BotoConfig

            # AWS 認証情報を取得
            credentials = config.get_aws_credentials()

            # None の値を除去
            filtered_credentials = {
                k: v for k, v in credentials.items() if v is not None
            }

            # タイムアウト設定を追加（ペルソナ生成は時間がかかるため長めに設定）
            boto_config = BotoConfig(
                connect_timeout=30,
                read_timeout=300,  # 5分（ペルソナ生成・議論に十分な時間）
                retries={"max_attempts": 0},  # リトライは自前で制御
            )

            # Bedrock Runtime クライアントを作成
            client = boto3.client(
                "bedrock-runtime", config=boto_config, **filtered_credentials
            )

            self.logger.info(
                f"Bedrock クライアントを作成しました (region: {config.AWS_REGION})"
            )
            return client

        except Exception as e:
            self.logger.error(f"Bedrock クライアントの作成に失敗: {e}")
            raise BedrockConnectionError(f"Bedrock クライアントの作成に失敗: {e}")

    def _retry_with_backoff(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """
        指数バックオフでリトライを実行
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
                    f"API エラーが発生しました。{delay}秒後にリトライします (試行 {attempt + 1}/{self.max_retries}): {e}"
                )
                time.sleep(delay)

        error_msg = (
            f"最大リトライ回数 ({self.max_retries}) に達しました: {last_exception}"
        )
        self.logger.error(error_msg)
        raise BedrockAPIError(error_msg)

    def _is_retryable_error(self, error: Exception) -> bool:
        """リトライ可能なエラーかどうかを判定"""
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

    def _invoke_model(self, prompt: str, max_tokens: Optional[int] = None) -> str:
        """Bedrock モデルを呼び出し

        Args:
            prompt: プロンプト文字列
            max_tokens: 最大トークン数（Noneの場合はデフォルト値を使用）
        """
        try:
            request_body = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
                "temperature": self.temperature,
                "messages": [{"role": "user", "content": prompt}],
            }

            response = self.bedrock_client.invoke_model(
                modelId=self.model_id,
                body=json.dumps(request_body),
                contentType="application/json",
            )

            response_body = json.loads(response["body"].read())

            if "content" in response_body and len(response_body["content"]) > 0:
                return str(response_body["content"][0]["text"])
            else:
                raise BedrockAPIError("モデルからの応答が空です")

        except json.JSONDecodeError as e:
            raise BedrockAPIError(f"レスポンスの JSON 解析に失敗: {e}")
        except KeyError as e:
            raise BedrockAPIError(f"レスポンス形式が不正です: {e}")
        except Exception as e:
            if isinstance(e, (BedrockAPIError, ClientError, BotoCoreError)):
                raise
            raise BedrockAPIError(f"モデル呼び出し中に予期しないエラーが発生: {e}")

    def _invoke_converse_api(
        self,
        messages: List[Dict[str, Any]],
        system_prompts: Optional[List[Dict[str, str]]] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """
        Bedrock Converse APIを呼び出し（マルチモーダル対応）

        Args:
            messages: メッセージリスト（content配列形式）
            system_prompts: システムプロンプト（オプション）

        Returns:
            str: モデルからの応答テキスト

        Raises:
            BedrockAPIError: API呼び出しエラー
        """
        try:
            request_params = {
                "modelId": self.model_id,
                "messages": messages,
                "inferenceConfig": {
                    "maxTokens": max_tokens or self.max_tokens,
                    "temperature": self.temperature,
                },
            }

            if system_prompts:
                request_params["system"] = system_prompts

            response = self.bedrock_client.converse(**request_params)

            # レスポンスから応答テキストを抽出
            if "output" in response and "message" in response["output"]:
                message = response["output"]["message"]
                if "content" in message and len(message["content"]) > 0:
                    return str(message["content"][0]["text"])

            raise BedrockAPIError("Converse APIからの応答が空です")

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))
            raise BedrockAPIError(
                f"Converse API呼び出しエラー ({error_code}): {error_msg}"
            )
        except Exception as e:
            if isinstance(e, BedrockAPIError):
                raise
            raise BedrockAPIError(
                f"Converse API呼び出し中に予期しないエラーが発生: {e}"
            )

    def _prepare_document_content(
        self, documents: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        ドキュメントをConverse API用のcontent形式に変換

        Args:
            documents: ドキュメントメタデータのリスト
                各辞書は以下のキーを含む:
                - file_path: ファイルパス（ローカルまたはs3://）
                - mime_type: MIMEタイプ
                - original_filename: 元のファイル名

        Returns:
            List[Dict[str, Any]]: Converse API用のcontentリスト

        Raises:
            AIServiceError: ドキュメント処理エラー
        """
        content_list = []

        for doc in documents:
            try:
                file_path = doc.get("file_path", "")
                mime_type = doc.get("mime_type", "")
                filename = doc.get("original_filename", "document")

                # ファイルを読み込み（S3またはローカル）
                if file_path.startswith("s3://"):
                    # S3から読み込み
                    from .service_factory import service_factory

                    s3_service = service_factory.get_s3_service()
                    if not s3_service:
                        raise AIServiceError(f"S3サービスが利用できません: {file_path}")

                    # S3パス全体を渡す
                    file_bytes = s3_service.download_file(file_path)
                else:
                    # ローカルファイルから読み込み
                    with open(file_path, "rb") as f:
                        file_bytes = f.read()

                # MIMEタイプに応じてcontent形式を決定
                if mime_type.startswith("image/"):
                    # 画像の場合（生のバイトデータを渡す）
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
                    # PDFの場合（生のバイトデータを渡す）
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
                    self.logger.warning(f"サポートされていないMIMEタイプ: {mime_type}")

            except FileNotFoundError:
                self.logger.error(f"ファイルが見つかりません: {file_path}")
                raise AIServiceError(
                    f"ドキュメントファイルが見つかりません: {file_path}"
                )
            except Exception as e:
                self.logger.error(f"ドキュメント処理エラー: {e}")
                raise AIServiceError(f"ドキュメント処理中にエラーが発生: {e}")

        return content_list

    def _facilitate_discussion_with_documents(
        self, personas: List[Persona], topic: str, documents: List[Dict[str, Any]]
    ) -> List[Message]:
        """
        ドキュメント付き議論を進行（Converse API使用）

        Args:
            personas: 議論参加ペルソナのリスト
            topic: 議論トピック
            documents: 添付ドキュメントのリスト

        Returns:
            List[Message]: 議論メッセージのリスト

        Raises:
            AIServiceError: 議論進行エラー
        """
        # ドキュメントをcontent形式に変換
        document_contents = self._prepare_document_content(documents)

        # 議論プロンプトを作成
        discussion_prompt = self._create_discussion_prompt(personas, topic)

        # メッセージを構築（テキスト + ドキュメント）
        content = [{"text": discussion_prompt}] + document_contents

        messages = [{"role": "user", "content": content}]

        # Converse APIを呼び出し
        response = self._retry_with_backoff(self._invoke_converse_api, messages)

        # レスポンスをパース
        parsed_messages = self._parse_discussion_response(response, personas)

        return parsed_messages

    def generate_persona(self, interview_text: str) -> Persona:
        """
        N1 インタビューテキストからペルソナを生成

        Args:
            interview_text: インタビューテキスト

        Returns:
            Persona: 生成されたペルソナオブジェクト

        Raises:
            AIServiceError: ペルソナ生成エラー
        """
        if not interview_text or not interview_text.strip():
            raise AIServiceError("インタビューテキストが空です")

        prompt = self._create_persona_generation_prompt(interview_text)

        try:
            response = self._retry_with_backoff(self._invoke_model, prompt)
            persona = self._parse_and_validate_persona(response)
            self.logger.info(f"ペルソナ生成が完了しました: {persona.name}")
            return persona
        except Exception as e:
            error_msg = f"ペルソナ生成中にエラーが発生: {e}"
            self.logger.error(error_msg)
            raise AIServiceError(error_msg)

    def _create_persona_generation_prompt(self, interview_text: str) -> str:
        """ペルソナ生成用のプロンプトを作成"""
        prompt = (
            """あなたはマーケティング専門家です。アップロードされたインタビューまたはその人物の発言テキストを詳細に分析して、リアルで具体的なペルソナを生成してください。

# インタビュー、発言テキスト
"""
            + interview_text
            + """

# 指示
インタビュー内容から読み取れる情報を基に、以下の要素を含む詳細なペルソナを作成してください：

1. **基本情報**: 名前、年齢、職業
2. **背景**: 生活環境、経歴、ライフスタイル
3. **価値観**: 大切にしていること、信念、優先順位
4. **課題・悩み**: 現在抱えている問題や不満
5. **目標・願望**: 達成したいこと、理想の状態

# 出力形式
**必ず以下のJSON形式のみで出力してください。説明文、前置き、後書きは一切不要です：**

{
    "name": "田中 花子",
    "age": 35,
    "occupation": "会社員（マーケティング部）",
    "background": "東京都在住。大学卒業後、現在の会社に就職し10年目。一人暮らしで、仕事とプライベートのバランスを重視している。",
    "values": ["効率性を重視する", "新しいことへの挑戦を大切にする", "人とのつながりを大事にする"],
    "pain_points": ["時間管理が難しい", "情報過多で選択に迷う", "仕事のストレスが多い"],
    "goals": ["キャリアアップを目指す", "ワークライフバランスを改善する", "新しいスキルを身につける"]
}

# 重要な注意事項
- 入力されたテキスト内容に基づいた現実的で具体的なペルソナを作成
- 日本人の名前を使用（多様な姓と名を使用）
- 年齢は数値のみ（引用符なし）
- 各リスト項目は3-5個程度
- JSON形式を厳密に守り、構文エラーがないようにする
- **出力は上記JSONのみで、他の文章は絶対に含めない**

JSON:"""
        )
        return prompt

    def _parse_and_validate_persona(self, response: str) -> Persona:
        """
        AI レスポンスからペルソナを解析・検証

        Args:
            response: AI からのレスポンス文字列

        Returns:
            Persona: 解析されたペルソナオブジェクト

        Raises:
            AIServiceError: 解析・検証エラー
        """
        try:
            # デバッグ用：AIレスポンス全体をログ出力
            self.logger.debug(f"AI レスポンス全体: {response}")

            # JSON 部分を抽出（ペルソナ生成ではオブジェクト形式を優先）
            json_str = self._extract_json_from_response(response, prefer_array=False)
            self.logger.debug(f"抽出されたJSON: {json_str}")

            # JSON をパース
            persona_data = json.loads(json_str)
            self.logger.debug(f"パースされたデータ: {persona_data}")

            # 必須フィールドの検証
            self._validate_persona_data(persona_data)

            # Persona オブジェクトを作成
            persona = Persona.create_new(
                name=persona_data["name"],
                age=int(persona_data["age"]),
                occupation=persona_data["occupation"],
                background=persona_data["background"],
                values=persona_data["values"],
                pain_points=persona_data["pain_points"],
                goals=persona_data["goals"],
            )

            return persona

        except json.JSONDecodeError as e:
            self.logger.error(f"JSON解析エラー - レスポンス: {response[:500]}...")
            # フォールバック：AIに再度JSONの修正を依頼
            try:
                self.logger.info("フォールバック処理を開始します")
                return self._retry_persona_generation_with_fix(response)
            except Exception as fallback_error:
                self.logger.error(f"フォールバック処理も失敗: {fallback_error}")
                raise AIServiceError(f"ペルソナの JSON 解析に失敗しました: {e}")
        except KeyError as e:
            self.logger.error(
                f"必須フィールド不足 - データ: {persona_data if 'persona_data' in locals() else 'N/A'}"
            )
            # フィールド不足の場合もフォールバックを試行
            try:
                self.logger.info(
                    "必須フィールド不足のためフォールバック処理を開始します"
                )
                return self._retry_persona_generation_with_fix(response)
            except Exception as fallback_error:
                self.logger.error(f"フォールバック処理も失敗: {fallback_error}")
                raise AIServiceError(f"必須フィールド '{e}' が不足しています")
        except ValueError as e:
            self.logger.error(
                f"データ形式エラー - データ: {persona_data if 'persona_data' in locals() else 'N/A'}"
            )
            raise AIServiceError(f"データ形式が不正です: {e}")
        except Exception as e:
            self.logger.error(f"予期しないエラー - レスポンス: {response[:500]}...")
            raise AIServiceError(f"ペルソナの解析中にエラーが発生しました: {e}")

    def _retry_persona_generation_with_fix(self, broken_response: str) -> Persona:
        """
        壊れたJSONレスポンスを修正してペルソナを生成

        Args:
            broken_response: 壊れたJSONレスポンス

        Returns:
            Persona: 修正されたペルソナオブジェクト

        Raises:
            AIServiceError: 修正に失敗した場合
        """
        self.logger.info("JSONレスポンスの修正を試行します")

        fix_prompt = f"""以下のレスポンスは有効なJSONではありません。これを修正して、有効なペルソナJSONを出力してください。

壊れたレスポンス:
{broken_response}

修正要件:
1. 有効なJSON形式にする
2. 必須フィールドを含める: name, age, occupation, background, values, pain_points, goals
3. 年齢は数値（引用符なし）
4. values, pain_points, goalsは文字列の配列

修正されたJSON:"""

        try:
            fixed_response = self._retry_with_backoff(self._invoke_model, fix_prompt)
            self.logger.debug(f"修正されたレスポンス: {fixed_response}")

            # 修正されたレスポンスを再度解析
            json_str = self._extract_json_from_response(fixed_response)
            persona_data = json.loads(json_str)
            self._validate_persona_data(persona_data)

            persona = Persona.create_new(
                name=persona_data["name"],
                age=int(persona_data["age"]),
                occupation=persona_data["occupation"],
                background=persona_data["background"],
                values=persona_data["values"],
                pain_points=persona_data["pain_points"],
                goals=persona_data["goals"],
            )

            self.logger.info("JSONレスポンスの修正に成功しました")
            return persona

        except Exception as e:
            self.logger.error(f"JSONレスポンスの修正に失敗: {e}")
            raise AIServiceError(f"JSONレスポンスの修正に失敗しました: {e}")

    def _extract_json_from_response(
        self, response: str, prefer_array: bool = False
    ) -> str:
        """
        レスポンスから JSON 部分を抽出

        Args:
            response: AI からのレスポンス文字列

        Returns:
            str: 抽出された JSON 文字列

        Raises:
            AIServiceError: JSON が見つからない場合
        """
        # レスポンスをクリーンアップ
        response = response.strip()

        if prefer_array:
            # 配列形式を優先（インサイト抽出用）
            array_start = response.find("[")
            if array_start != -1:
                # 対応する閉じ括弧を探す
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
                    # 抽出したJSONが有効かテスト
                    try:
                        json.loads(json_candidate)
                        return json_candidate
                    except json.JSONDecodeError:
                        self.logger.warning(
                            f"配列JSON候補が無効: {json_candidate[:100]}..."
                        )

            # 配列が見つからない場合はオブジェクトを探す
            start_idx = response.find("{")
            if start_idx != -1:
                # 対応する閉じ括弧を探す
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
                    # 抽出したJSONが有効かテスト
                    try:
                        json.loads(json_candidate)
                        return json_candidate
                    except json.JSONDecodeError:
                        self.logger.warning(
                            f"オブジェクトJSON候補が無効: {json_candidate[:100]}..."
                        )
        else:
            # オブジェクト形式を優先（ペルソナ生成用）
            start_idx = response.find("{")
            if start_idx != -1:
                # 対応する閉じ括弧を探す
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
                    # 抽出したJSONが有効かテスト
                    try:
                        json.loads(json_candidate)
                        return json_candidate
                    except json.JSONDecodeError:
                        self.logger.warning(
                            f"オブジェクトJSON候補が無効: {json_candidate[:100]}..."
                        )

            # オブジェクトが見つからない場合は配列を探す
            array_start = response.find("[")
            if array_start != -1:
                # 対応する閉じ括弧を探す
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
                    # 抽出したJSONが有効かテスト
                    try:
                        json.loads(json_candidate)
                        return json_candidate
                    except json.JSONDecodeError:
                        self.logger.warning(
                            f"配列JSON候補が無効: {json_candidate[:100]}..."
                        )

        # JSONブロック（```json ... ```）を探す
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
                        f"JSONブロック候補が無効: {json_candidate[:100]}..."
                    )

        # 最後の手段：レスポンス全体がJSONかチェック
        try:
            json.loads(response)
            return response
        except json.JSONDecodeError:
            pass

        # すべて失敗した場合
        self.logger.error(f"JSON抽出失敗 - レスポンス全体: {response}")
        raise AIServiceError(
            f"レスポンスから有効なJSONを抽出できませんでした。レスポンス: {response[:200]}..."
        )

    def _validate_persona_data(self, data: Dict[str, Any]) -> None:
        """
        ペルソナデータの検証

        Args:
            data: 検証するペルソナデータ

        Raises:
            AIServiceError: 検証エラー
        """
        # 必須フィールドの存在確認
        required_fields = [
            "name",
            "age",
            "occupation",
            "background",
            "values",
            "pain_points",
            "goals",
        ]
        for field in required_fields:
            if field not in data:
                raise AIServiceError(f"必須フィールド '{field}' が不足しています")

        # データ型の検証
        if not isinstance(data["name"], str) or not data["name"].strip():
            raise AIServiceError("名前は空でない文字列である必要があります")

        if not isinstance(data["age"], (int, str)):
            raise AIServiceError("年齢は数値である必要があります")

        # 年齢を整数に変換して範囲チェック
        try:
            age = int(data["age"])
            if age < 0 or age > 150:
                raise AIServiceError("年齢は0から150の範囲である必要があります")
        except (ValueError, TypeError):
            raise AIServiceError("年齢は有効な数値である必要があります")

        if not isinstance(data["occupation"], str) or not data["occupation"].strip():
            raise AIServiceError("職業は空でない文字列である必要があります")

        if not isinstance(data["background"], str) or not data["background"].strip():
            raise AIServiceError("背景は空でない文字列である必要があります")

        # リスト型フィールドの検証
        list_fields = ["values", "pain_points", "goals"]
        for field in list_fields:
            if not isinstance(data[field], list):
                raise AIServiceError(f"'{field}' はリスト形式である必要があります")

            if len(data[field]) == 0:
                raise AIServiceError(f"'{field}' は少なくとも1つの要素が必要です")

            for item in data[field]:
                if not isinstance(item, str) or not item.strip():
                    raise AIServiceError(
                        f"'{field}' の各要素は空でない文字列である必要があります"
                    )

    def facilitate_discussion(
        self,
        personas: List[Persona],
        topic: str,
        documents: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Message]:
        """
        ペルソナ同士の議論を進行

        Args:
            personas: 議論参加ペルソナのリスト
            topic: 議論トピック
            documents: 添付ドキュメントのリスト（オプション）

        Returns:
            List[Message]: 議論メッセージのリスト

        Raises:
            AIServiceError: 議論進行エラー
        """
        if not personas or len(personas) < 2:
            raise AIServiceError("議論には最低2つのペルソナが必要です")

        if len(personas) > 5:
            raise AIServiceError("議論参加ペルソナは最大5つまでです")

        if not topic or not topic.strip():
            raise AIServiceError("議論トピックが空です")

        # トピックの長さチェック
        if len(topic.strip()) > 200:
            raise AIServiceError("議論トピックは200文字以内で入力してください")

        self.logger.info(
            f"議論を開始します (参加者: {len(personas)}人, トピック: {topic[:50]}..., ドキュメント: {len(documents) if documents else 0}件)"
        )

        # ドキュメントがある場合はConverse APIを使用
        if documents:
            try:
                messages = self._facilitate_discussion_with_documents(
                    personas, topic, documents
                )

                # 各ペルソナが最低1回は発言していることを確認
                persona_message_count: dict[str, int] = {}
                for message in messages:
                    persona_message_count[message.persona_id] = (
                        persona_message_count.get(message.persona_id, 0) + 1
                    )

                for persona in personas:
                    if persona_message_count.get(persona.id, 0) == 0:
                        self.logger.warning(
                            f"ペルソナ {persona.name} の発言が見つかりませんでした"
                        )

                self.logger.info(
                    f"議論が完了しました (メッセージ数: {len(messages)}, 参加者別発言数: {persona_message_count})"
                )
                return messages

            except AIServiceError:
                raise
            except Exception as e:
                error_msg = f"ドキュメント付き議論進行中にエラーが発生: {e}"
                self.logger.error(error_msg)
                raise AIServiceError(error_msg)

        # ドキュメントがない場合は従来のメソッドを使用
        prompt = self._create_discussion_prompt(personas, topic)

        try:
            response = self._retry_with_backoff(self._invoke_model, prompt)
            messages = self._parse_discussion_response(response, personas)

            # 各ペルソナが最低1回は発言していることを確認
            persona_message_count = {}
            for message in messages:
                persona_message_count[message.persona_id] = (
                    persona_message_count.get(message.persona_id, 0) + 1
                )

            for persona in personas:
                if persona_message_count.get(persona.id, 0) == 0:
                    self.logger.warning(
                        f"ペルソナ {persona.name} の発言が見つかりませんでした"
                    )

            self.logger.info(
                f"議論が完了しました (メッセージ数: {len(messages)}, 参加者別発言数: {persona_message_count})"
            )
            return messages

        except AIServiceError:
            # AIServiceError は再発生
            raise
        except Exception as e:
            error_msg = f"議論進行中にエラーが発生: {e}"
            self.logger.error(error_msg)
            raise AIServiceError(error_msg)

    def facilitate_discussion_streaming(
        self,
        personas: List[Persona],
        topic: str,
        documents: Optional[List[Dict[str, Any]]] = None,
    ) -> Any:
        """
        ペルソナ同士の議論を進行（ストリーミング版）
        各発言をyieldで返す

        Args:
            personas: 議論参加ペルソナのリスト
            topic: 議論トピック
            documents: オプションのドキュメントリスト（file_path, mime_typeを含む）

        Yields:
            Message: 各発言メッセージ

        Raises:
            AIServiceError: 議論進行エラー
        """
        if not personas or len(personas) < 2:
            raise AIServiceError("議論には最低2つのペルソナが必要です")

        if len(personas) > 5:
            raise AIServiceError("議論参加ペルソナは最大5つまでです")

        if not topic or not topic.strip():
            raise AIServiceError("議論トピックが空です")

        if len(topic.strip()) > 200:
            raise AIServiceError("議論トピックは200文字以内で入力してください")

        self.logger.info(f"ストリーミング議論を開始します (参加者: {len(personas)}人)")

        prompt = self._create_discussion_prompt(personas, topic)
        persona_map = {persona.name: persona.id for persona in personas}

        try:
            # ドキュメントがある場合はconverse_streamを使用
            if documents:
                # ドキュメントコンテンツを準備
                document_contents = self._prepare_document_content(documents)

                # メッセージコンテンツを構築
                message_content = [{"text": prompt}] + document_contents

                response = self.bedrock_client.converse_stream(
                    modelId=self.model_id,
                    messages=[{"role": "user", "content": message_content}],
                    inferenceConfig={
                        "maxTokens": self.max_tokens,
                        "temperature": self.temperature,
                    },
                )

                # ストリーミングレスポンスを処理
                accumulated_text = ""
                yielded_messages = set()

                for event in response.get("stream", []):
                    if "contentBlockDelta" in event:
                        delta = event["contentBlockDelta"].get("delta", {})
                        if "text" in delta:
                            accumulated_text += delta["text"]

                            # 完成したメッセージを検出してyield
                            lines = accumulated_text.split("\n")
                            for i, line in enumerate(
                                lines[:-1]
                            ):  # 最後の行は未完成の可能性
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
                return

            # ドキュメントがない場合は従来のストリーミングAPI
            request_body = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "messages": [{"role": "user", "content": prompt}],
            }

            response = self.bedrock_client.invoke_model_with_response_stream(
                modelId=self.model_id,
                body=json.dumps(request_body),
                contentType="application/json",
            )

            # ストリームからテキストを蓄積して発言を検出
            buffer = ""
            for event in response.get("body", []):
                chunk = event.get("chunk")
                if chunk:
                    chunk_data = json.loads(chunk.get("bytes", b"{}").decode())
                    if chunk_data.get("type") == "content_block_delta":
                        delta = chunk_data.get("delta", {})
                        text = delta.get("text", "")
                        buffer += text
                        # print(buffer)

                        # 改行で区切って完成した発言を検出
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            print(line)
                            if line and line.startswith("[") and "]:" in line:
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
                                        print(message)
                                        yield message
                                except (ValueError, IndexError):
                                    continue

            # バッファに残った最後の発言を処理
            if buffer.strip():
                line = buffer.strip()
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
                            yield message
                    except (ValueError, IndexError):
                        pass

            self.logger.info("ストリーミング議論が完了しました")

        except Exception as e:
            error_msg = f"ストリーミング議論中にエラーが発生: {e}"
            self.logger.error(error_msg)
            raise AIServiceError(error_msg)

    def extract_insights(
        self,
        discussion_messages: List[Message],
        categories: Optional[List[InsightCategory]] = None,
        topic: str = "",
    ) -> List[Dict[str, Any]]:
        """
        議論メッセージからインサイトを抽出

        Args:
            discussion_messages: 議論メッセージのリスト
            categories: インサイトカテゴリーのリスト（Noneの場合はデフォルトを使用）

        Returns:
            List[Dict[str, Any]]: 抽出されたインサイトの構造化データ
                各辞書は以下のキーを含む:
                - category: str (カテゴリー)
                - description: str (説明)
                - confidence_score: float (信頼度スコア 0.0-1.0)

        Raises:
            AIServiceError: インサイト抽出エラー
        """
        if not discussion_messages:
            raise AIServiceError("議論メッセージが空です")

        if len(discussion_messages) < 2:
            raise AIServiceError("インサイト抽出には最低2つのメッセージが必要です")

        # メッセージの総文字数チェック
        total_chars = sum(len(msg.content) for msg in discussion_messages)
        if total_chars < 50:
            raise AIServiceError("議論内容が短すぎます。より詳細な議論が必要です")

        # カテゴリーが指定されていない場合はデフォルトを使用
        if categories is None:
            categories = config.get_default_insight_categories()

        self.logger.info(
            f"インサイト抽出を開始します (メッセージ数: {len(discussion_messages)}, 総文字数: {total_chars}, カテゴリー数: {len(categories)})"
        )

        prompt = self._create_insight_extraction_prompt(discussion_messages, categories, topic)

        try:
            response = self._retry_with_backoff(self._invoke_model, prompt)
            self.logger.debug(f"インサイト抽出AIレスポンス: {response[:500]}...")
            insights = self._parse_structured_insights_response(response, categories)

            # インサイトの品質チェック
            if len(insights) < 3:
                self.logger.warning(
                    f"抽出されたインサイト数が少なすぎます: {len(insights)}"
                )
                self.logger.warning(f"AIレスポンス全文: {response}")
                # 警告は出すが、エラーにはしない

            # 重複チェック
            unique_insights = []
            seen_descriptions = set()
            for insight in insights:
                description_lower = insight["description"].lower().strip()
                if (
                    description_lower not in seen_descriptions
                    and len(insight["description"].strip()) > 10
                ):
                    unique_insights.append(insight)
                    seen_descriptions.add(description_lower)

            if len(unique_insights) != len(insights):
                self.logger.info(
                    f"重複インサイトを除去しました (元: {len(insights)}, 除去後: {len(unique_insights)})"
                )

            self.logger.info(
                f"インサイト抽出が完了しました (インサイト数: {len(unique_insights)})"
            )
            return unique_insights

        except AIServiceError:
            # AIServiceError は再発生
            raise
        except Exception as e:
            error_msg = f"インサイト抽出中にエラーが発生: {e}"
            self.logger.error(error_msg)
            raise AIServiceError(error_msg)

    def _create_discussion_prompt(self, personas: List[Persona], topic: str) -> str:
        """議論進行用のプロンプトを作成"""
        personas_info = []
        for persona in personas:
            persona_dict = asdict(persona)
            persona_info = f"\n**{persona_dict['name']}**\n"
            persona_info += f"- 年齢: {persona_dict['age']}歳\n"
            persona_info += f"- 職業: {persona_dict['occupation']}\n"
            persona_info += f"- 背景: {persona_dict['background']}\n"
            persona_info += f"- 価値観: {', '.join(persona_dict['values'])}\n"
            persona_info += (
                f"- 抱えている課題: {', '.join(persona_dict['pain_points'])}\n"
            )
            persona_info += f"- 目標・願望: {', '.join(persona_dict['goals'])}\n"
            personas_info.append(persona_info)

        personas_text = "\n".join(personas_info)

        prompt = f"""あなたはマーケティング専門家として、以下のペルソナたちによる「{topic}」についての議論をファシリテートしてください。

# 参加ペルソナ
{personas_text}

# 議論の進行方針
1. **多角的な視点**: 各ペルソナの価値観、背景、課題、目標に基づいた異なる視点を反映
2. **建設的な対話**: 単なる意見の対立ではなく、相互理解と新たな気づきを生む議論
3. **実践的な内容**: 商品企画やマーケティング戦略に活用できる具体的な議論
4. **自然な流れ**: リアルな会話として成立する自然な議論の進行

# 議論の構成
- 各ペルソナが4-5回ずつ発言
- 最初は各自の立場や考えを表明
- 中盤では他のペルソナの意見に対する反応や質問
- 終盤では議論を通じた気づきや結論の共有

# 出力形式
以下の形式で厳密に出力してください。他の説明文は一切含めないでください：

[{personas[0].name}]: 発言内容
[{personas[1].name}]: 発言内容
[{personas[0].name}]: 発言内容
...

# 重要な注意事項
- 各ペルソナの個性と特徴を明確に区別して表現
- 発言内容は具体的で実践的な内容にする
- ペルソナ名は必ず角括弧で囲む、氏名だけで職業など不要なものは角括弧に絶対に含めないこと
- 発言内容は自然で現実的な会話にする
- 議論の質を高めるため、深い洞察や具体例を含める

議論を開始してください。"""
        return prompt

    def _create_insight_extraction_prompt(
        self, messages: List[Message], categories: Optional[List[InsightCategory]],
        topic: str = "",
    ) -> str:
        """インサイト抽出用のプロンプトを作成"""
        # ペルソナの最終ラウンド発言とファシリテータの要約を分離
        max_round = max((msg.round_number or 0) for msg in messages)
        persona_final_statements = [
            f"**{msg.persona_name}**: {msg.content}"
            for msg in messages
            if msg.persona_id != "facilitator" and (msg.round_number or 0) > max_round - 3
        ]
        facilitator_summaries = [
            f"ラウンド{msg.round_number}: {msg.content}"
            for msg in messages
            if msg.persona_id == "facilitator"
        ]

        persona_text = "\n".join(persona_final_statements)
        facilitator_text = "\n".join(facilitator_summaries) if facilitator_summaries else ""

        # カテゴリーがNoneの場合はデフォルトを使用
        if categories is None:
            from src.config import Config

            categories = Config().get_default_insight_categories()

        # カテゴリーセクションを動的に生成
        categories_section = ""
        for i, category in enumerate(categories, 1):
            categories_section += f"\n## {i}. {category.name}\n"
            categories_section += f"- {category.description}\n"

        # カテゴリー名のリストを生成（JSON例とバリデーション用）
        category_names = [cat.name for cat in categories]
        category_names_str = "、".join([f'"{name}"' for name in category_names])

        topic_section = f"\n# 議論テーマと目的\n{topic}\n" if topic else ""

        prompt = f"""以下のペルソナ議論を分析し、議論テーマの目的に沿った実践的なインサイトを抽出してください。
{topic_section}
# ペルソナの直近の発言
{persona_text}
"""
        if facilitator_text:
            prompt += f"""
# 各ラウンドのファシリテータ要約（議論の流れ）
{facilitator_text}
"""

        prompt += f"""
# インサイト抽出の観点
以下の{len(categories)}つのカテゴリーから、議論内容に基づいた具体的で実践的なインサイトを抽出してください：
{categories_section}

# 信頼度スコアの基準
各インサイトには以下の基準で信頼度スコア（0.0-1.0）を付与してください：

- **0.9-1.0 (非常に高い)**: 複数のペルソナが明確に言及し、具体的な根拠がある
- **0.7-0.8 (高い)**: 議論の中で明確に表現され、十分な根拠がある
- **0.5-0.6 (中程度)**: 議論から推測できるが、間接的な根拠
- **0.3-0.4 (低い)**: 議論の文脈から読み取れるが、推測の要素が強い
- **0.1-0.2 (非常に低い)**: 一般的な推測に基づく

# 出力形式
以下のJSON形式で正確に出力してください。他の説明文は一切含めないでください：

[
    {{
        "category": "{category_names[0]}",
        "description": "具体的で実践的なインサイトの内容",
        "confidence_score": 0.85
    }},
    {{
        "category": "{category_names[1] if len(category_names) > 1 else category_names[0]}",
        "description": "具体的で実践的なインサイトの内容",
        "confidence_score": 0.72
    }}
]

# 重要な注意事項
- 各インサイトは議論内容に基づいた根拠のある内容にする
- 抽象的な表現ではなく、具体的で実行可能な示唆を提供
- 議論テーマの目的に沿った実務で活用できるレベルの詳細度
- 最低3個、最大10個程度のインサイトを抽出
- カテゴリーは{category_names_str}のいずれかを使用
- 信頼度スコアは議論内容の根拠の強さに基づいて適切に設定
- JSON形式を厳密に守り、構文エラーがないようにする
- 出力はJSONのみで、他の説明や前置きは不要

インサイトの抽出を開始してください。"""
        return prompt

    def _parse_discussion_response(
        self, response: str, personas: List[Persona]
    ) -> List[Message]:
        """議論レスポンスをメッセージリストに解析"""
        messages = []
        lines = response.strip().split("\n")

        # ペルソナ名のマッピングを作成
        persona_map = {persona.name: persona.id for persona in personas}

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # [ペルソナ名]: 発言内容 の形式を解析
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
                    # 解析に失敗した行はスキップ
                    self.logger.warning(
                        f"議論レスポンスの解析に失敗した行をスキップ: {line}"
                    )
                    continue

        # 最低限のメッセージ数をチェック
        if len(messages) < 2:
            self.logger.warning(
                f"解析されたメッセージ数が少なすぎます: {len(messages)}"
            )
            raise AIServiceError(
                "議論の解析結果が不十分です。各ペルソナから最低1つずつのメッセージが必要です。"
            )

        return messages

    def _parse_structured_insights_response(
        self, response: str, categories: List[InsightCategory]
    ) -> List[Dict[str, Any]]:
        """
        構造化されたインサイトレスポンスを解析

        Args:
            response: AI からのレスポンス文字列（JSON形式）
            categories: 有効なインサイトカテゴリーのリスト

        Returns:
            List[Dict[str, Any]]: 解析されたインサイトの構造化データ

        Raises:
            AIServiceError: 解析エラー
        """
        try:
            # JSON 部分を抽出（インサイト抽出では配列形式を優先）
            json_str = self._extract_json_from_response(response, prefer_array=True)

            # JSON をパース
            insights_data = json.loads(json_str)

            if not isinstance(insights_data, list):
                raise AIServiceError("インサイトデータはリスト形式である必要があります")

            # 各インサイトを検証・正規化
            validated_insights = []
            for i, insight in enumerate(insights_data):
                try:
                    validated_insight = self._validate_and_normalize_insight(
                        insight, i, categories
                    )
                    validated_insights.append(validated_insight)
                except Exception as e:
                    self.logger.warning(f"インサイト {i + 1} の検証に失敗: {e}")
                    continue

            if not validated_insights:
                raise AIServiceError("有効なインサイトが見つかりませんでした")

            return validated_insights

        except json.JSONDecodeError as e:
            self.logger.warning(f"JSON解析に失敗、フォールバック処理を実行: {e}")
            # フォールバック: 従来の文字列解析を使用
            return self._fallback_to_text_parsing(response)
        except Exception as e:
            self.logger.error(f"構造化インサイト解析エラー: {e}")
            # フォールバック: 従来の文字列解析を使用
            return self._fallback_to_text_parsing(response)

    def _validate_and_normalize_insight(
        self, insight: Dict[str, Any], index: int, categories: List[InsightCategory]
    ) -> Dict[str, Any]:
        """
        インサイトデータを検証・正規化

        Args:
            insight: 検証するインサイトデータ
            index: インサイトのインデックス（エラーメッセージ用）
            categories: 有効なインサイトカテゴリーのリスト

        Returns:
            Dict[str, Any]: 検証・正規化されたインサイトデータ

        Raises:
            AIServiceError: 検証エラー
        """
        if not isinstance(insight, dict):
            raise AIServiceError(
                f"インサイト {index + 1} は辞書形式である必要があります"
            )

        # 必須フィールドの確認
        required_fields = ["category", "description", "confidence_score"]
        for field in required_fields:
            if field not in insight:
                raise AIServiceError(
                    f"インサイト {index + 1} に必須フィールド '{field}' がありません"
                )

        # カテゴリーの検証・正規化
        category = str(insight["category"]).strip()
        valid_categories = [cat.name for cat in categories]

        # カテゴリー名の正規化（部分一致を許可）
        normalized_category = None
        for valid_cat in valid_categories:
            if valid_cat in category or category in valid_cat:
                normalized_category = valid_cat
                break

        if not normalized_category:
            # デフォルトカテゴリーを設定（最初のカテゴリーまたは「その他」）
            default_category = valid_categories[-1] if valid_categories else "その他"
            normalized_category = default_category
            self.logger.warning(
                f"インサイト {index + 1} のカテゴリー '{category}' が無効なため、'{default_category}' に設定しました"
            )

        # 説明の検証
        description = str(insight["description"]).strip()
        if not description or len(description) < 10:
            raise AIServiceError(
                f"インサイト {index + 1} の説明が短すぎます（最低10文字必要）"
            )

        # 信頼度スコアの検証・正規化
        try:
            confidence_score = float(insight["confidence_score"])
            if not 0.0 <= confidence_score <= 1.0:
                # 範囲外の場合は0.0-1.0に正規化
                confidence_score = max(0.0, min(1.0, confidence_score))
                self.logger.warning(
                    f"インサイト {index + 1} の信頼度スコアを正規化しました: {confidence_score}"
                )
        except (ValueError, TypeError):
            # 無効な値の場合はデフォルト値を設定
            confidence_score = 0.5
            self.logger.warning(
                f"インサイト {index + 1} の信頼度スコアが無効なため、デフォルト値 0.5 を設定しました"
            )

        return {
            "category": normalized_category,
            "description": description,
            "confidence_score": confidence_score,
        }

    def _fallback_to_text_parsing(self, response: str) -> List[Dict[str, Any]]:
        """
        フォールバック: 従来のテキスト解析を使用してインサイトを抽出

        Args:
            response: AI からのレスポンス文字列

        Returns:
            List[Dict[str, Any]]: 解析されたインサイトの構造化データ
        """
        self.logger.info(
            "フォールバック処理: テキスト解析を使用してインサイトを抽出します"
        )

        # 従来のメソッドを使用
        text_insights = self._parse_insights_response(response)

        # テキストインサイトを構造化データに変換
        structured_insights = []
        for insight_text in text_insights:
            category, description = self._extract_category_and_description(insight_text)

            # デフォルトの信頼度スコアを設定（議論の長さに基づいて調整）
            default_confidence = 0.6  # 中程度の信頼度

            structured_insights.append(
                {
                    "category": category,
                    "description": description,
                    "confidence_score": default_confidence,
                }
            )

        self.logger.info(
            f"フォールバック処理完了: {len(structured_insights)}個のインサイトを抽出"
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

    def _parse_insights_response(self, response: str) -> List[str]:
        """インサイトレスポンスを解析

        対応形式:
        - 箇条書き: `- [カテゴリー] 内容` または `- 内容`
        - 番号付き: `1. [カテゴリー] 内容` または `1. 内容`
        - 太字カテゴリ（Claude形式）: `**[カテゴリー]** 内容` または `**カテゴリー**: 内容`
        - カテゴリのみ: `[カテゴリー] 内容`
        - 見出し形式: `## カテゴリー` の後に内容
        """
        insights = []
        lines = response.strip().split("\n")
        current_category = None

        for line in lines:
            line = line.strip()
            if not line:
                continue

            insight = None

            # 見出し形式のカテゴリを検出 (## や ### で始まる行)
            if line.startswith("#"):
                # カテゴリ名を抽出して保存
                current_category = line.lstrip("#").strip()
                continue

            # 1. 箇条書きの記号を除去
            if line.startswith(("- ", "• ", "* ", "・")):
                content = line[2:].strip()
                if content:
                    insight = content
            # 2. 番号付きリストの場合
            elif line.startswith(
                tuple(f"{i}." for i in range(1, 10))
            ) or line.startswith(tuple(f"{i})" for i in range(1, 10))):
                parts = line.split(".", 1) if "." in line[:3] else line.split(")", 1)
                if len(parts) > 1:
                    insight = parts[1].strip()
            # 3. Claude形式: **[カテゴリー]** で始まる場合
            elif line.startswith("**[") and "]**" in line:
                insight = line.replace("**[", "[").replace("]**", "]")
            # 4. Claude形式: **カテゴリー**: 内容
            elif line.startswith("**") and "**:" in line:
                # **カテゴリー**: 内容 → [カテゴリー] 内容
                parts = line.split("**:", 1)
                if len(parts) == 2:
                    category = parts[0].replace("**", "").strip()
                    content = parts[1].strip()
                    if content:
                        insight = f"[{category}] {content}"
            # 5. [カテゴリー] で始まる場合（箇条書き記号なし）
            elif line.startswith("[") and "]" in line:
                insight = line
            # 6. 太字で始まる行（カテゴリなしの場合）
            elif line.startswith("**") and line.endswith("**"):
                # 見出しとして扱う
                current_category = line.strip("*").strip()
                continue
            # 7. 通常のテキスト行（十分な長さがあれば）
            elif len(line) > 20 and not line.startswith(("#", "---", "===", "```")):
                # カテゴリがあれば付与
                if current_category:
                    insight = f"[{current_category}] {line}"
                else:
                    insight = line

            if insight and len(insight) > 10:
                insights.append(insight)

        return insights

    def generate_discussion_report(
        self,
        messages: List[Message],
        insights: List[Dict[str, Any]],
        topic: str,
        template_type: str,
        custom_prompt: Optional[str] = None,
        personas: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        議論データからテンプレートに基づくレポートを生成

        Args:
            messages: 議論メッセージのリスト
            insights: 抽出済みインサイトのリスト
            topic: 議論トピック
            template_type: テンプレート種別 ("summary", "review", "custom")
            custom_prompt: カスタムプロンプト (template_type == "custom" の場合)
            personas: 参加ペルソナのプロフィール情報

        Returns:
            str: 生成されたレポート（Markdown形式）
        """
        system_prompt = self._build_report_system_prompt(
            topic, template_type, custom_prompt
        )
        converse_messages = self._build_report_context(
            messages, insights, topic, personas
        )

        return self._invoke_converse_api(
            converse_messages, system_prompts=[{"text": system_prompt}],
            max_tokens=12000,
        )

    def generate_discussion_report_streaming(
        self,
        messages: List[Message],
        insights: List[Dict[str, Any]],
        topic: str,
        template_type: str,
        custom_prompt: Optional[str] = None,
        personas: Optional[List[Dict[str, Any]]] = None,
    ) -> Any:
        """
        議論データからレポートをストリーミング生成する。

        Yields:
            str: テキストチャンク
        """
        system_prompt = self._build_report_system_prompt(
            topic, template_type, custom_prompt
        )
        converse_messages = self._build_report_context(
            messages, insights, topic, personas
        )

        def _call_stream() -> Any:
            return self.bedrock_client.converse_stream(
                modelId=self.model_id,
                messages=converse_messages,
                system=[{"text": system_prompt}],
                inferenceConfig={
                    "maxTokens": 12000,
                    "temperature": self.temperature,
                },
            )

        response = self._retry_with_backoff(_call_stream)

        for event in response.get("stream", []):
            if "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                if "text" in delta:
                    yield delta["text"]

    def _build_report_context(
        self,
        messages: List[Message],
        insights: List[Dict[str, Any]],
        topic: str,
        personas: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """レポート生成用のコンテキストメッセージを構築する"""
        context_parts = [f"## 議論トピック\n{topic}\n"]

        if personas:
            context_parts.append("## 参加ペルソナのプロフィール")
            for p in personas:
                context_parts.append(
                    f"### {p['name']}（{p['age']}歳 / {p['occupation']}）\n"
                    f"- 価値観: {', '.join(p.get('values', []))}\n"
                    f"- 課題: {', '.join(p.get('pain_points', []))}\n"
                    f"- 目標: {', '.join(p.get('goals', []))}"
                )

        context_parts.append("\n## 議論ログ")
        for msg in messages:
            context_parts.append(f"**{msg.persona_name}**: {msg.content}")
        context_parts.append("\n## 抽出済みインサイト")
        for ins in insights:
            context_parts.append(
                f"- [{ins.get('category', '')}] {ins.get('description', '')} (信頼度: {ins.get('confidence_score', 0)})"
            )

        user_content = "\n".join(context_parts)
        return [{"role": "user", "content": [{"text": user_content}]}]

    def _build_report_system_prompt(
        self, topic: str, template_type: str, custom_prompt: Optional[str] = None
    ) -> str:
        """テンプレート種別に応じたシステムプロンプトを構築"""
        base = (
            f"あなたは定性調査の分析専門家です。"
            f"以下の議論・インタビューデータ（トピック: {topic}）を分析し、"
            f"施策やアクションに繋がる実用的なレポートを生成してください。"
            f"出力はMarkdown形式で記述してください。"
            f"簡潔かつ要点を絞った記述を心がけ、冗長な説明は避けてください。"
        )

        if template_type == "summary":
            return (
                f"{base}\n\n"
                "以下の構成でレポートを作成してください:\n\n"
                "## 1. エグゼクティブサマリ\n"
                "議論全体の要点を3-5行で簡潔にまとめる。\n\n"
                "## 2. 参加ペルソナの概要\n"
                "各ペルソナの属性と特徴的な価値観・課題を2-3行で紹介。\n\n"
                "## 3. 主要な発見（Key Findings）\n"
                "3-5個に絞り、各発見について以下を記述:\n"
                "- **根拠**: どのペルソナの具体的な発言か（引用）\n"
                "- **背景**: その発言の背景にある価値観や課題\n"
                "- **合意度**: 他のペルソナも同意しているか、対立意見はあるか\n"
                "- **意味合い**: この発見がビジネスにとって何を意味するか\n\n"
                "## 4. 示唆と推奨アクション\n"
                "各施策について「どの発見から導かれたか」を明記し、表形式で整理。\n"
                "表の列: 根拠となる発見 / 施策内容 / 対象セグメント / 期待効果（具体的に） / 優先度（高/中/低）。5件以内。\n\n"
                "## 5. 追加調査が必要な領域\n"
                "2-3個を箇条書きで、ペルソナ間で意見が分かれた点や検証が不十分な仮説を挙げる。"
            )
        elif template_type == "review":
            return (
                f"{base}\n\n"
                "レビューコメント形式で出力してください。5-10件に絞ってください。\n"
                "各コメントには以下を含めてください:\n"
                "- **該当箇所**: 議論中の具体的な発言や論点\n"
                "- **指摘内容**: その箇所から読み取れる課題・機会・リスク\n"
                "- **重要度**: 高/中/低\n"
                "- **推奨アクション**: この指摘に対して取るべき具体的なアクション\n\n"
                "表形式で整理してください。"
            )
        elif template_type == "custom" and custom_prompt:
            return f"{base}\n\nユーザーからの指示:\n{custom_prompt}"
        else:
            return base

    # =========================================================================
    # アンケートAI生成（Issue #23）
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
        """[{role, content}] -> Converse API用のメッセージ配列に変換"""
        converted: List[Dict[str, Any]] = []
        for m in messages:
            role = m.get("role", "user")
            content = (m.get("content") or "").strip()
            if not content or role not in ("user", "assistant"):
                continue
            converted.append({"role": role, "content": [{"text": content}]})
        return converted

    def chat_for_survey(self, messages: List[Dict[str, str]]) -> str:
        """アンケートヒアリング用のマルチターン会話。

        Args:
            messages: [{"role": "user"|"assistant", "content": "..."}, ...] の会話履歴
                     （最後のメッセージは user 発言であることを想定）

        Returns:
            assistantの返答テキスト
        """
        if not messages:
            raise AIServiceError("会話履歴が空です")
        if messages[-1].get("role") != "user":
            raise AIServiceError("最後のメッセージは user である必要があります")

        converse_messages = self._to_converse_messages(messages)
        if not converse_messages:
            raise AIServiceError("有効なメッセージがありません")

        try:
            return str(
                self._retry_with_backoff(
                    self._invoke_converse_api,
                    converse_messages,
                    system_prompts=[{"text": self._SURVEY_CHAT_SYSTEM_PROMPT}],
                    max_tokens=1024,
                )
            )
        except AIServiceError:
            raise
        except Exception as e:
            raise AIServiceError(f"アンケートヒアリング中にエラーが発生: {e}")

    def generate_survey_questions_draft(
        self, messages: List[Dict[str, str]]
    ) -> Dict[str, Any]:
        """会話履歴から設問ドラフトを生成して JSON 辞書を返す。

        Returns:
            {"summary": str, "questions": [ {question_type, text, options, allow_multiple, max_selections}, ... ]}
        """
        if not messages:
            raise AIServiceError("会話履歴が空です")

        # 会話履歴 + 「ドラフト生成指示」を最後に追加
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

        try:
            response = self._retry_with_backoff(
                self._invoke_converse_api,
                converse_messages,
                system_prompts=[{"text": self._SURVEY_DRAFT_SYSTEM_PROMPT}],
                max_tokens=4096,
            )
        except AIServiceError:
            raise
        except Exception as e:
            raise AIServiceError(f"設問ドラフト生成中にエラーが発生: {e}")

        try:
            json_str = self._extract_json_from_response(response, prefer_array=False)
            data = json.loads(json_str)
        except (AIServiceError, json.JSONDecodeError) as e:
            self.logger.error(f"ドラフトJSON解析失敗: {e} / response={response[:500]}")
            raise AIServiceError(f"設問ドラフトのJSON解析に失敗: {e}")

        if not isinstance(data, dict) or "questions" not in data:
            raise AIServiceError(
                "設問ドラフトのJSONに 'questions' フィールドがありません"
            )
        if not isinstance(data["questions"], list) or not data["questions"]:
            raise AIServiceError(
                "設問ドラフトの 'questions' が空またはリストではありません"
            )

        data["summary"] = str(data.get("summary", "") or "").strip()
        data["template_name"] = str(data.get("template_name", "") or "").strip()[:50]
        return data
