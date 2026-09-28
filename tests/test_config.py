from decimal import Decimal

import pytest

from cof_bot.config import STABLECOIN_BASES, load_settings
from cof_bot.errors import ConfigError


def test_defaults_are_shadow_and_unauthenticated():
    s = load_settings({})
    assert s.trading_mode == "shadow"
    assert not s.has_credentials
    assert s.excluded_bases == frozenset({"BTC", "ETH"})
    assert STABLECOIN_BASES <= s.all_excluded_bases


def test_secret_never_in_repr():
    s = load_settings({"COINBASE_API_KEY": "organizations/x/apiKeys/abcdef123456", "COINBASE_API_SECRET": "TOPSECRET"})
    assert s.has_credentials
    assert "TOPSECRET" not in repr(s)
    assert "abcdef123456" not in repr(s)
    assert s.masked_key() == "...123456"


def test_key_without_secret_rejected():
    with pytest.raises(ConfigError):
        load_settings({"COINBASE_API_KEY": "k"})


def test_key_file_and_key_both_rejected(tmp_path):
    f = tmp_path / "key.json"
    f.write_text("{}")
    with pytest.raises(ConfigError):
        load_settings({"COINBASE_API_KEY_FILE": str(f), "COINBASE_API_KEY": "k", "COINBASE_API_SECRET": "s"})


def test_missing_key_file_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_settings({"COINBASE_API_KEY_FILE": str(tmp_path / "nope.json")})


@pytest.mark.parametrize(
    "env",
    [
        {"COF_TRADING_MODE": "yolo"},
        {"COF_MAX_RETRIES": "-1"},
        {"COF_MAX_RETRIES": "two"},
        {"COF_REST_MAX_RPS": "0"},
        {"COF_MIN_QUOTE_VOLUME_24H": "-5"},
        {"COF_MIN_QUOTE_VOLUME_24H": "NaN"},
        {"COF_MIN_QUOTE_VOLUME_24H": "100", "COF_MAX_QUOTE_VOLUME_24H": "50"},
        {"COF_BACKOFF_BASE_S": "10", "COF_BACKOFF_CAP_S": "5"},
    ],
)
def test_invalid_values_rejected(env):
    with pytest.raises(ConfigError):
        load_settings(env)


def test_parsing():
    s = load_settings(
        {
            "COF_TRADING_MODE": "LIVE",
            "COF_EXCLUDED_BASES": " btc, sol ,",
            "COF_MIN_QUOTE_VOLUME_24H": "0",
            "COF_MAX_QUOTE_VOLUME_24H": "2000000",
        }
    )
    assert s.trading_mode == "live"
    assert s.excluded_bases == frozenset({"BTC", "SOL"})
    assert s.min_quote_volume_24h == Decimal("0")
    assert s.max_quote_volume_24h == Decimal("2000000")


def test_dotenv_loaded_but_real_env_wins(tmp_path, monkeypatch):
    envfile = tmp_path / ".env"
    envfile.write_text("COF_MAX_RETRIES=7\nCOF_REQUEST_TIMEOUT_S=3\n")
    monkeypatch.setenv("COF_MAX_RETRIES", "2")
    monkeypatch.delenv("COF_REQUEST_TIMEOUT_S", raising=False)
    for name in ("COINBASE_API_KEY", "COINBASE_API_SECRET", "COINBASE_API_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    s = load_settings(dotenv_path=str(envfile))
    assert s.max_retries == 2
    assert s.request_timeout_s == 3
