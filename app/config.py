from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class MailboxConfig(BaseModel):
    name: str
    user: str
    app_password: str
    brand: str
    default_lang: str
    db_path: Path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    imap_host: str = "mail.infomaniak.com"
    imap_port: int = 993

    mailbox_1_name: str
    mailbox_1_user: str
    mailbox_1_app_password: str
    mailbox_1_brand: str
    mailbox_1_default_lang: str = "fr"

    mailbox_2_name: str
    mailbox_2_user: str
    mailbox_2_app_password: str
    mailbox_2_brand: str
    mailbox_2_default_lang: str = "en"

    mailbox_3_name: str
    mailbox_3_user: str
    mailbox_3_app_password: str
    mailbox_3_brand: str
    mailbox_3_default_lang: str = "fr"

    ollama_pro_api_key: str = ""
    ollama_pro_base_url: str = "https://ollama.com/api"
    llm_model_default: str = "ollama_chat/kimi-k2"
    openrouter_api_key: str = ""
    llm_model_fallback: str = "openrouter/anthropic/claude-sonnet-4"
    llm_model_classifier: str = "ollama_chat/kimi-k2"

    resend_api_key: str = ""
    resend_from: str = "agent@digitalhs.biz"
    draft_recipient: str = "cdal@digitalhs.biz"

    embedding_model: str = "intfloat/multilingual-e5-large"
    rag_top_k: int = 5

    poll_interval_seconds: int = 300

    data_dir: Path = Path("./data")
    db_boite_1: Path = Path("./data/boite1.sqlite")
    db_boite_2: Path = Path("./data/boite2.sqlite")
    db_boite_3: Path = Path("./data/boite3.sqlite")
    db_agent_state: Path = Path("./data/agent_state.db")

    healthcheck_host: str = "127.0.0.1"
    healthcheck_port: int = 8765

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_bot_name: str = "Charlie"

    slack_webhook_url: str = ""

    dry_run: bool = False

    log_level: str = "INFO"

    # --- Web UI ---
    web_secret_key: str = ""
    web_encryption_key: str = ""
    web_session_ttl_hours: int = 24
    web_bind_host: str = "127.0.0.1"
    web_bind_port: int = 8080
    public_base_url: str = ""  # ex: https://detective.digitalhs.biz
    admin_email: str = "cdal@digitalhs.biz"
    operator_email: str = "contact@detectivebelgique.be"
    magic_link_ttl_minutes: int = 15

    def mailboxes(self) -> list[MailboxConfig]:
        return [
            MailboxConfig(
                name=self.mailbox_1_name,
                user=self.mailbox_1_user,
                app_password=self.mailbox_1_app_password,
                brand=self.mailbox_1_brand,
                default_lang=self.mailbox_1_default_lang,
                db_path=self.db_boite_1,
            ),
            MailboxConfig(
                name=self.mailbox_2_name,
                user=self.mailbox_2_user,
                app_password=self.mailbox_2_app_password,
                brand=self.mailbox_2_brand,
                default_lang=self.mailbox_2_default_lang,
                db_path=self.db_boite_2,
            ),
            MailboxConfig(
                name=self.mailbox_3_name,
                user=self.mailbox_3_user,
                app_password=self.mailbox_3_app_password,
                brand=self.mailbox_3_brand,
                default_lang=self.mailbox_3_default_lang,
                db_path=self.db_boite_3,
            ),
        ]


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
