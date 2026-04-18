"""Typed configuration. Values come from env vars (see .env.example)."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Global ---
    forge_log_level: str = "INFO"
    forge_data_dir: Path = Path("data")
    forge_binance_ws_url: str = "wss://stream.binance.com:9443/ws"
    forge_nexus_relay_url: str = "http://localhost:8080"
    forge_nexus_post_daily: bool = True

    # --- Hyperliquid bucket ---
    hl_ws_url: str = "wss://api.hyperliquid.xyz/ws"
    hl_rest_url: str = "https://api.hyperliquid.xyz"
    hl_bucket_id: str = "hyperliquid_paper"
    hl_starting_bankroll_usd: float = 10_000.0
    hl_notional_usd: float = 10_000.0
    hl_leverage: float = 3.0
    hl_taker_fee_bps: float = 2.5
    hl_slippage_bps: float = 1.5
    hl_daily_loss_cap_pct: float = 0.20
    hl_circuit_loss_threshold: int = 3
    hl_circuit_cooldown_periods: int = 4
    hl_paper_only: bool = True

    # Phase 2 wallet identity (optional; Phase 1 never reads these).
    hl_wallet_address: str = ""
    hl_wallet_private_key: str = ""  # MUST come from env/secret-store; never commit

    @property
    def taker_fee_frac(self) -> float:
        return self.hl_taker_fee_bps / 10_000.0

    @property
    def slippage_frac(self) -> float:
        return self.hl_slippage_bps / 10_000.0


# Lazy singleton so tests can override via env before first access.
_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_for_tests() -> None:
    global _settings
    _settings = None
