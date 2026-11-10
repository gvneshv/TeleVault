"""Request and response models for api/routes/ingest.py (the ingestion-time chat blocklist and the message ignore rules)."""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from utils.validation import IGNORE_RULE_MAX_CHATS, IGNORE_RULE_MAX_COUNT, IGNORE_RULE_MAX_LENGTH, TELEGRAM_CHAT_ID_MAX, TELEGRAM_CHAT_ID_MIN


class BlockedChatOut(BaseModel):
    """One chat the account owner chose not to archive."""

    chat_id: int
    name: str | None = Field(
        None,
        description="The chat's name when it was blocked. Kept on the rule itself, so it is still shown after the chat's own data is gone.",
    )
    blocked_at: datetime


class BlocklistOut(BaseModel):
    """GET /ingest/blocklist."""

    items: list[BlockedChatOut]
    total: int
    max_count: int = Field(description="The most chats one account may block (utils/validation.py's INGEST_BLOCKLIST_MAX_COUNT).")


class BlockedChatRemovedOut(BaseModel):
    """DELETE /ingest/blocklist/{chat_id}: `removed` is False when the chat was not blocked in the first place (the call is idempotent)."""

    chat_id: int
    removed: bool


class IgnoreRuleIn(BaseModel):
    """
    POST /ingest/ignore-rules: a phrase, and where it applies - either every chat (`all_chats`) or the listed `chat_ids`, never both and never neither.

    The phrase is trimmed before it is checked, so a rule cannot be made of spaces.
    It is compared as a plain substring ignoring case (utils/text_matching.py).
    """

    pattern: str = Field(min_length=1, max_length=IGNORE_RULE_MAX_LENGTH, description="The phrase to ignore.")
    all_chats: bool = False
    chat_ids: list[int] = Field(default_factory=list, max_length=IGNORE_RULE_MAX_CHATS)

    @field_validator("pattern", mode="before")
    @classmethod
    def _trim(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("pattern")
    @classmethod
    def _no_nul(cls, value: str) -> str:
        # Postgres text cannot hold a NUL character; without this it would surface as a 500 from the driver.
        if "\x00" in value:
            raise ValueError("must not contain a NUL character")
        return value

    @field_validator("chat_ids")
    @classmethod
    def _bounded_unique_ids(cls, value: list[int]) -> list[int]:
        for chat_id in value:
            if not TELEGRAM_CHAT_ID_MIN <= chat_id <= TELEGRAM_CHAT_ID_MAX:
                raise ValueError("not a Telegram chat id")
        return list(dict.fromkeys(value))  # drop repeats, keep the order

    @model_validator(mode="after")
    def _exactly_one_scope(self) -> "IgnoreRuleIn":
        if self.all_chats == bool(self.chat_ids):
            raise ValueError("choose either all_chats or at least one chat id, not both and not neither")
        return self


class IgnoreRuleChatOut(BaseModel):
    """A chat an ignore rule is limited to."""

    chat_id: int
    name: str | None = Field(None, description="The chat's name when the rule was created (the rule keeps its own copy).")


class IgnoreRuleOut(BaseModel):
    """One message ignore rule."""

    id: int
    pattern: str
    all_chats: bool
    chats: list[IgnoreRuleChatOut] = Field(description="Empty for an all-chats rule.")
    created_at: datetime


class IgnoreRulesOut(BaseModel):
    """GET /ingest/ignore-rules, with the limits the Settings form needs (so web/ never repeats the numbers)."""

    items: list[IgnoreRuleOut]
    total: int
    max_count: int = Field(description=f"The most rules one account may have (default {IGNORE_RULE_MAX_COUNT}).")
    max_length: int = Field(description="The longest phrase, in characters (utils/validation.py's IGNORE_RULE_MAX_LENGTH).")
    max_chats: int = Field(description="The most chats one rule may be limited to.")


class IgnoreRuleRemovedOut(BaseModel):
    """DELETE /ingest/ignore-rules/{id}: `removed` is False when there was no such rule (the call is idempotent)."""

    id: int
    removed: bool
