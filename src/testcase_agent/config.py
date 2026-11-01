from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1"
    data_dir: Path = Path("data")
    app_token: str = ""
    allowed_hosts: str = "localhost,127.0.0.1,[::1],testserver"
    google_credentials_file: str = ""
    dropbox_access_token: str = ""
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""
    allowed_download_hosts: str = ""
    poll_seconds: int = 0
    max_file_bytes: int = 20 * 1024 * 1024
    max_bundle_bytes: int = 60 * 1024 * 1024
    max_documents: int = 12
    max_pages: int = 60
    max_text_chars: int = 160_000
    max_images: int = 60
