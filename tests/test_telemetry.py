import pytest

from compendium import telemetry


def test_cost_uses_current_anthropic_rates():
    # 1M in + 1M out on Opus 5 = $5 + $25.
    assert telemetry.cost_usd("claude-opus-5", 1_000_000, 1_000_000) == pytest.approx(30.0)


def test_cost_is_none_for_unpriced_models():
    """Better to report 'unknown' than to quote a fabricated number."""
    assert telemetry.cost_usd("some-model-we-have-not-priced", 1000, 1000) is None


async def test_record_persists_even_when_the_call_raises(monkeypatch):
    captured = {}

    async def fake_execute(sql, params=()):
        captured["params"] = params

    monkeypatch.setattr(telemetry.db, "execute", fake_execute)

    with pytest.raises(ValueError):
        async with telemetry.record("claude-opus-5", "answer") as call:
            call.tokens_in = 100
            raise ValueError("provider blew up")

    params = captured["params"]
    assert params[0] == "claude-opus-5"
    assert params[2] == 100
    assert "ValueError" in params[-1]
