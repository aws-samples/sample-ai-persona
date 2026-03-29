"""
InsightCategory data model for the AI Persona System.
"""

from dataclasses import dataclass
from typing import Dict, Any, List
import json


@dataclass
class InsightCategory:
    """
    Represents a category for insight extraction.
    """

    name: str
    description: str

    def __post_init__(self) -> None:
        if not self.name or len(self.name.strip()) == 0:
            raise ValueError("Category name cannot be empty")
        if len(self.name) > 50:
            raise ValueError("Category name must be 50 characters or less")
        if len(self.description) > 500:
            raise ValueError("Category description must be 500 characters or less")

    @classmethod
    def create_new(cls, name: str, description: str) -> "InsightCategory":
        """Create a new InsightCategory instance with validation."""
        return cls(name=name.strip(), description=description.strip())

    def to_dict(self) -> Dict[str, Any]:
        """Convert InsightCategory to dictionary for serialization."""
        return {"name": self.name, "description": self.description}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "InsightCategory":
        """Create InsightCategory instance from dictionary."""
        return cls(name=data["name"], description=data["description"])

    def to_json(self) -> str:
        """Convert InsightCategory to JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> "InsightCategory":
        """Create InsightCategory instance from JSON string."""
        data = json.loads(json_str)
        return cls.from_dict(data)

    @classmethod
    def get_default_categories(cls) -> List["InsightCategory"]:
        """Get the default insight categories."""
        return [
            cls(
                name="顧客ニーズ",
                description="議論から読み取れる潜在的・顕在的ニーズ、各ペルソナが抱える共通課題や個別課題、現在のソリューションに対する不満や改善要望",
            ),
            cls(
                name="市場機会",
                description="議論から見えてくる新たな市場セグメント、競合他社が見落としている機会、成長が期待できる領域や需要",
            ),
            cls(
                name="商品開発",
                description="議論内容から導かれる機能要件や仕様、ユーザー体験（UX）改善のヒント、新商品・サービスのアイデア",
            ),
            cls(
                name="マーケティング",
                description="効果的なメッセージングやポジショニング、適切なコミュニケーションチャネル、プロモーション戦略のヒント",
            ),
            cls(
                name="その他",
                description="上記以外で戦略的に重要な洞察、業界トレンドや将来予測、リスクや注意すべき点",
            ),
        ]
