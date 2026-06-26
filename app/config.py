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
    imap_host: str
    imap_port: int
    short_code: str
    cerveau2_marque: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Host IMAP global (fallback quand une boîte n'a pas de host dédié).
    # v1.27.0 : les 3 boîtes Infomaniak utilisent ce fallback ; la 4ème boîte OVH
    # utilise son propre MAILBOX_4_IMAP_HOST.
    imap_host: str = "mail.infomaniak.com"
    imap_port: int = 993

    mailbox_1_name: str
    mailbox_1_user: str
    mailbox_1_app_password: str
    mailbox_1_brand: str
    mailbox_1_default_lang: str = "fr"
    mailbox_1_imap_host: str = ""
    mailbox_1_imap_port: int = 993
    mailbox_1_short_code: str = "D_FR"
    mailbox_1_cerveau2_marque: str = "detectivebelgique"

    mailbox_2_name: str
    mailbox_2_user: str
    mailbox_2_app_password: str
    mailbox_2_brand: str
    mailbox_2_default_lang: str = "en"
    mailbox_2_imap_host: str = ""
    mailbox_2_imap_port: int = 993
    mailbox_2_short_code: str = "D_NL"
    mailbox_2_cerveau2_marque: str = "detectivebelgium"

    mailbox_3_name: str
    mailbox_3_user: str
    mailbox_3_app_password: str
    mailbox_3_brand: str
    mailbox_3_default_lang: str = "fr"
    mailbox_3_imap_host: str = ""
    mailbox_3_imap_port: int = 993
    mailbox_3_short_code: str = "D_PD"
    mailbox_3_cerveau2_marque: str = "dpdhu"

    # v1.27.0 — 4ème boîte (OVH) : info@detectives-belgique.be
    mailbox_4_name: str = "detectives_belgique"
    mailbox_4_user: str = "info@detectives-belgique.be"
    mailbox_4_app_password: str = ""
    mailbox_4_brand: str = "Detectives Belgique"
    mailbox_4_default_lang: str = "fr"
    mailbox_4_imap_host: str = "ex5.mail.ovh.net"
    mailbox_4_imap_port: int = 993
    mailbox_4_short_code: str = "D_DS"
    mailbox_4_cerveau2_marque: str = "detectivesbelgique"

    ollama_pro_api_key: str = ""
    ollama_pro_base_url: str = "https://ollama.com/v1"
    # v1.25.0 : bascule modèles — gemma4:31b (non-reasoning, multimodal) est le
    # modèle principal sur toutes les tâches (default/classifier/chat/qualifier).
    # Fallback = glm-5.2:cloud (reasoning model, thinking High/Max). kimi-k2.6:cloud
    # n'est plus utilisé. Provider Ollama Pro Cloud = openai/<model> + api_base
    # ollama.com/v1 (JAMAIS ollama_chat/<model> → force vers Ollama local inexistant).
    openrouter_api_key: str = ""
    llm_model_default: str = "openai/gemma4:31b"
    llm_model_fallback: str = "openai/glm-5.2:cloud"
    llm_model_classifier: str = "openai/gemma4:31b"
    llm_model_chat: str = "openai/gemma4:31b"
    # v1.22.7 : modèle dédié à la qualification prospect (cas de figure + questions)
    llm_model_qualifier: str = "openai/gemma4:31b"

    resend_api_key: str = ""
    resend_from: str = "agent@digitalhs.biz"
    # v1.21.7 : fallback Resend pour brouillon — Daniel en to, CDAL en cc
    # (avant : tout allait à CDAL, Daniel ne voyait jamais le brouillon en fallback)
    draft_recipient: str = "cdal@digitalhs.biz"  # legacy, conservé pour alertes
    draft_recipient_to: str = "contact@detectivebelgique.be"
    draft_recipient_cc: str = "cdal@digitalhs.biz"

    embedding_model: str = "openai/text-embedding-3-small"
    embedding_api_base: str = "https://openrouter.ai/api/v1"
    embedding_api_key: str = ""  # utilise openrouter_api_key si vide
    rag_top_k: int = 10  # v1.22.0 : 5 → 10 — plus de cas historiques au LLM
    # v1.24.2 : RAG mis en pause par défaut. L'approche déterministe
    # (qualification_builder + few-shot Daniel) est plus fiable et remplace le
    # RAG pour la génération des brouillons. Le RAG était de plus cassé sur les
    # 3 boîtes depuis le 2026-05-28 (point de vigilance #1). Réactivable via
    # RAG_ENABLED=true (utile uniquement si on re-bootstrap pairs_vec).
    rag_enabled: bool = False

    poll_interval_seconds: int = 300

    # --- Poller — seuil d'alerte erreurs consécutives (v1.21.3) ---
    # Au-dessus de N crashes successifs sur 1 boîte → email Resend à cdal@digitalhs.biz.
    # Ajustable code uniquement (pas env). Anti-spam 1h/boîte côté alerts.py.
    poller_alert_threshold: int = 5

    # --- Tarifs qualification prospect (v1.22.7) ---
    # Utilisés dans les brouillons de réponse pour demande_client et prise_contact.
    # Modifiables via .env ou runtime via app_settings.
    dossier_opening_fee: int = 200
    report_fee: int = 150
    hourly_rate_day: int = 75
    hourly_rate_night_weekend: int = 95

    # --- Catégories qui déclenchent la génération d'un brouillon (v1.22.7) ---
    draft_categories: str = "demande_client,prise_contact"

    data_dir: Path = Path("./data")
    db_boite_1: Path = Path("./data/boite1.sqlite")
    db_boite_2: Path = Path("./data/boite2.sqlite")
    db_boite_3: Path = Path("./data/boite3.sqlite")
    # v1.27.0 : nouvelle boîte OVH detectives-belgique.be
    db_boite_4: Path = Path("./data/boite4.sqlite")
    db_agent_state: Path = Path("./data/agent_state.db")

    healthcheck_host: str = "127.0.0.1"
    healthcheck_port: int = 8765

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_bot_name: str = "Charlie"

    slack_webhook_url: str = ""

    slack_bot_token: str = ""  # xoxb-... Bot User OAuth Token
    slack_signing_secret: str = ""  # Signing secret de l'app Slack

    cerveau2_base_url: str = ""
    cerveau2_api_secret: str = ""
    cerveau2_limit: int = 8  # v1.22.0 : 3 → 8 — plus de notes Vault au LLM

    dry_run: bool = False

    # --- Date limite de traitement ---
    # Format ISO : 2026-06-01 — Charlie ne traite que les mails reçus depuis cette date.
    # Vide = pas de filtre (tout l'historique).
    process_since_date: str = ""  # ex: 2026-06-01

    log_level: str = "INFO"
    log_dir: Path = Path("./logs")

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

    def _mailbox_config(
        self,
        name: str,
        user: str,
        app_password: str,
        brand: str,
        default_lang: str,
        db_path: Path,
        imap_host: str,
        imap_port: int,
        short_code: str,
        cerveau2_marque: str,
    ) -> MailboxConfig:
        return MailboxConfig(
            name=name,
            user=user,
            app_password=app_password,
            brand=brand,
            default_lang=default_lang,
            db_path=db_path,
            imap_host=imap_host or self.imap_host,
            imap_port=imap_port or self.imap_port,
            short_code=short_code,
            cerveau2_marque=cerveau2_marque,
        )

    def mailboxes(self) -> list[MailboxConfig]:
        return [
            self._mailbox_config(
                name=self.mailbox_1_name,
                user=self.mailbox_1_user,
                app_password=self.mailbox_1_app_password,
                brand=self.mailbox_1_brand,
                default_lang=self.mailbox_1_default_lang,
                db_path=self.db_boite_1,
                imap_host=self.mailbox_1_imap_host,
                imap_port=self.mailbox_1_imap_port,
                short_code=self.mailbox_1_short_code,
                cerveau2_marque=self.mailbox_1_cerveau2_marque,
            ),
            self._mailbox_config(
                name=self.mailbox_2_name,
                user=self.mailbox_2_user,
                app_password=self.mailbox_2_app_password,
                brand=self.mailbox_2_brand,
                default_lang=self.mailbox_2_default_lang,
                db_path=self.db_boite_2,
                imap_host=self.mailbox_2_imap_host,
                imap_port=self.mailbox_2_imap_port,
                short_code=self.mailbox_2_short_code,
                cerveau2_marque=self.mailbox_2_cerveau2_marque,
            ),
            self._mailbox_config(
                name=self.mailbox_3_name,
                user=self.mailbox_3_user,
                app_password=self.mailbox_3_app_password,
                brand=self.mailbox_3_brand,
                default_lang=self.mailbox_3_default_lang,
                db_path=self.db_boite_3,
                imap_host=self.mailbox_3_imap_host,
                imap_port=self.mailbox_3_imap_port,
                short_code=self.mailbox_3_short_code,
                cerveau2_marque=self.mailbox_3_cerveau2_marque,
            ),
            self._mailbox_config(
                name=self.mailbox_4_name,
                user=self.mailbox_4_user,
                app_password=self.mailbox_4_app_password,
                brand=self.mailbox_4_brand,
                default_lang=self.mailbox_4_default_lang,
                db_path=self.db_boite_4,
                imap_host=self.mailbox_4_imap_host,
                imap_port=self.mailbox_4_imap_port,
                short_code=self.mailbox_4_short_code,
                cerveau2_marque=self.mailbox_4_cerveau2_marque,
            ),
        ]


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
