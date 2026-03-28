from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Database
    database_url: str = "sqlite:///claims.db"

    # Company info
    company_name: str = "Acme Logistics GmbH"
    company_address: str = "Mariahilfer Straße 100, 1060 Wien, Austria"
    company_email: str = "claims@acme-logistics.at"
    company_phone: str = "+43 1 555 1234"
    company_contact_person: str = "Max Mustermann"

    # IMAP (email ingestion)
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    imap_processed_folder: str = "Processed"
    imap_use_ssl: bool = True

    # SMTP (notifications)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    notification_email: str = ""

    # Scheduler
    schedule_hour: int = 8
    schedule_minute: int = 0

    # Ingestion
    csv_watch_dir: str = "import"

    # LLM (OpenRouter)
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str = "nvidia/nemotron-3-super-120b-a12b:free"

    # BrowserBase (headless browser for tracking)
    browserbase_api_key: str = ""
    browserbase_project_id: str = ""

    # Web dashboard
    web_host: str = "0.0.0.0"
    web_port: int = 8000


settings = Settings()
