from app.config import settings


def test_nonprod_tigerbay_forces_dry_run(monkeypatch):
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, "allow_writes_from_nonprod_tigerbay", False)
    monkeypatch.setattr(settings, "tigerbay_base_url", "https://beachcomber-preproduction.ontigerbay.co.uk/nimble")
    assert settings.effective_dry_run()
    monkeypatch.setattr(settings, "tigerbay_base_url", "https://beachcomber.ontigerbay.co.uk/nimble")
    assert not settings.effective_dry_run()
    monkeypatch.setattr(settings, "tigerbay_base_url", "https://beachcomber-preview.ontigerbay.co.uk/nimble")
    monkeypatch.setattr(settings, "allow_writes_from_nonprod_tigerbay", True)
    assert not settings.effective_dry_run()
