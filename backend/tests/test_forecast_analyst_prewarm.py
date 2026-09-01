"""Engine analyst pre-warm hook."""
from app.services.forecasting import engine as engine_mod


def test_prewarm_calls_service_for_ecisco_product(monkeypatch):
    calls = []
    monkeypatch.setattr(engine_mod, "_get_config_bool",
                        lambda key, default=True: key == "FORECAST_ANALYST_LLM_ENABLED")
    from app.services.forecasting.analyst import service as analyst_service
    monkeypatch.setattr(analyst_service, "prewarm_brief",
                        lambda pid, day, db: calls.append((pid, day)))
    engine_mod._prewarm_analyst_brief(db=None, product_key="ecisco.dcpd")
    assert calls == [("dcpd", 7)]


def test_prewarm_skips_when_flag_off(monkeypatch):
    calls = []
    monkeypatch.setattr(engine_mod, "_get_config_bool", lambda key, default=True: False)
    from app.services.forecasting.analyst import service as analyst_service
    monkeypatch.setattr(analyst_service, "prewarm_brief",
                        lambda pid, day, db: calls.append((pid, day)))
    engine_mod._prewarm_analyst_brief(db=None, product_key="ecisco.dcpd")
    assert calls == []


def test_prewarm_skips_non_ecisco_keys(monkeypatch):
    calls = []
    monkeypatch.setattr(engine_mod, "_get_config_bool", lambda key, default=True: True)
    from app.services.forecasting.analyst import service as analyst_service
    monkeypatch.setattr(analyst_service, "prewarm_brief",
                        lambda pid, day, db: calls.append((pid, day)))
    engine_mod._prewarm_analyst_brief(db=None, product_key="other.something")
    engine_mod._prewarm_analyst_brief(db=None, product_key=None)
    assert calls == []


def test_prewarm_swallows_service_errors(monkeypatch):
    monkeypatch.setattr(engine_mod, "_get_config_bool", lambda key, default=True: True)
    from app.services.forecasting.analyst import service as analyst_service

    def _boom(pid, day, db):
        raise RuntimeError("LLM down")
    monkeypatch.setattr(analyst_service, "prewarm_brief", _boom)
    engine_mod._prewarm_analyst_brief(db=None, product_key="ecisco.dcpd")  # must not raise
