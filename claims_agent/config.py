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


settings = Settings()
