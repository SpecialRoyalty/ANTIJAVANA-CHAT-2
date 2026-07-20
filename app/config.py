from __future__ import annotations
from functools import lru_cache
from typing import Optional, List
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')
    bot_token: str = Field(alias='BOT_TOKEN')
    database_url: str = Field(alias='DATABASE_URL')
    admin_ids: List[int] = Field(default_factory=list, alias='ADMIN_IDS')
    trusted_ids: List[int] = Field(default_factory=list, alias='TRUSTED_IDS')
    main_group_id: int = Field(alias='MAIN_GROUP_ID')
    pass_soiree_group_id: Optional[int] = Field(default=None, alias='PASS_SOIREE_GROUP_ID')
    pass_total_group_id: Optional[int] = Field(default=None, alias='PASS_TOTAL_GROUP_ID')
    vip_javana_group_id: Optional[int] = Field(default=None, alias='VIP_JAVANA_GROUP_ID')
    log_group_id: Optional[int] = Field(default=None, alias='LOG_GROUP_ID')
    public_bot_username: str = Field(default='', alias='PUBLIC_BOT_USERNAME')
    timezone: str = Field(default='Europe/Paris', alias='TIMEZONE')
    default_vote_goal: int = Field(default=120, alias='DEFAULT_VOTE_GOAL')
    default_time_slot: str = Field(default='22:30-00:45', alias='DEFAULT_TIME_SLOT')
    auto_schedule_enabled: bool = Field(default=True, alias='AUTO_SCHEDULE_ENABLED')
    paypal_text: str = Field(default='', alias='PAYPAL_TEXT')
    revolut_text: str = Field(default='', alias='REVOLUT_TEXT')
    crypto_text: str = Field(default='', alias='CRYPTO_TEXT')

    @field_validator('database_url', mode='before')
    @classmethod
    def fix_db_url(cls, v):
        if isinstance(v, str):
            if v.startswith('postgres://'):
                v = 'postgresql://' + v[len('postgres://'):]
            if v.startswith('postgresql://'):
                v = 'postgresql+asyncpg://' + v[len('postgresql://'):]
        return v

    @field_validator('admin_ids','trusted_ids', mode='before')
    @classmethod
    def parse_ids(cls, v):
        if v is None or v == '': return []
        if isinstance(v, list): return [int(x) for x in v]
        if isinstance(v, int): return [v]
        if isinstance(v, str): return [int(x.strip()) for x in v.split(',') if x.strip()]
        return []

    @field_validator('pass_soiree_group_id','pass_total_group_id','vip_javana_group_id','log_group_id', mode='before')
    @classmethod
    def empty_int(cls, v):
        if v is None or v == '': return None
        return int(v)

    @property
    def all_admin_ids(self) -> set[int]:
        return set(self.admin_ids) | set(self.trusted_ids)

@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore
