from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    check_interval: int = 60

    class Config:
        env_file = ".env"


settings = Settings()
