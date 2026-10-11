"""An explicit provider quota rejection is distinct from an uncertain call."""

from types import SimpleNamespace

import pytest

from auto_agents.control import ControlError
from auto_agents.control.effects import Provider
from auto_agents.models import AgentResult, AgentTermination, ProjectConfig, ProviderConfig


QUOTA = "You've hit your usage limit. Try again later."


@pytest.mark.parametrize("ok", [False, True])
@pytest.mark.parametrize("message", [QUOTA, "usage_limit_exceeded", "insufficient_quota", "Rate limit exceeded", "额度耗尽"])
def test_confirmed_quota_rejection_survives_provider_error_termination(tmp_path, monkeypatch, ok, message):
    config = ProjectConfig("fixture")
    config.providers["quota"] = ProviderConfig(kind="mock", binary="fixture")
    native = AgentResult(ok, ["fixture"], tmp_path / "response", stderr=message,
                         termination=AgentTermination(reason="provider_error"))
    provider = Provider(config)
    monkeypatch.setattr(provider, "adapter", lambda alias: SimpleNamespace(
        available=lambda: True, run=lambda request: native,
    ))
    ctx = SimpleNamespace(provider="quota", workspace_root=tmp_path, environment={},
                          contract=SimpleNamespace(inputs={}))
    with pytest.raises(ControlError) as caught:
        provider.run(ctx, "prototype_prepare", "Prepare", tmp_path / "response")
    assert caught.value.code == "provider_quota" and caught.value.category == "provider"
    assert message in str(caught.value)


@pytest.mark.parametrize("reason,incomplete,message", [
    ("provider_error", True, QUOTA), ("provider_idle", False, QUOTA),
    ("external_interrupt", False, QUOTA), ("provider_error", False, "Connection lost"),
    ("provider_error", False, "Connection lost; check the credits dashboard"),
    ("provider_error", False, "Connection lost; quota status unavailable"),
    ("provider_error", False, QUOTA + "\nConnection lost"),
])
def test_unconfirmed_or_unclean_calls_still_require_reconciliation(tmp_path, monkeypatch, reason, incomplete, message):
    config = ProjectConfig("fixture")
    config.providers["quota"] = ProviderConfig(kind="mock", binary="fixture")
    native = AgentResult(False, ["fixture"], tmp_path / "response", stderr=message,
                         termination=AgentTermination(reason=reason), cleanup_incomplete=incomplete)
    provider = Provider(config)
    monkeypatch.setattr(provider, "adapter", lambda alias: SimpleNamespace(
        available=lambda: True, run=lambda request: native,
    ))
    ctx = SimpleNamespace(provider="quota", workspace_root=tmp_path, environment={},
                          contract=SimpleNamespace(inputs={}))
    with pytest.raises(ControlError) as caught:
        provider.run(ctx, "prototype_prepare", "Prepare", tmp_path / "response")
    assert caught.value.code == "outcome_unknown" and caught.value.category == "reconciliation"


def test_mid_acceptance_quota_cannot_authorize_repeating_external_work(tmp_path, monkeypatch):
    config = ProjectConfig("fixture")
    config.providers["quota"] = ProviderConfig(kind="mock", binary="fixture")
    native = AgentResult(False, ["fixture"], tmp_path / "response", stderr=QUOTA,
                         termination=AgentTermination(reason="provider_error"))
    provider = Provider(config)
    monkeypatch.setattr(provider, "adapter", lambda alias: SimpleNamespace(
        available=lambda: True, run=lambda request: native,
    ))
    ctx = SimpleNamespace(provider="quota", workspace_root=tmp_path, environment={},
                          contract=SimpleNamespace(inputs={}))
    with pytest.raises(ControlError) as caught:
        provider.run(ctx, "acceptance", "Verify real external work", tmp_path / "response")
    assert caught.value.code == "outcome_unknown" and caught.value.category == "reconciliation"


@pytest.mark.parametrize("max_calls", [1, 12])
def test_quota_settles_as_failure_and_keeps_call_accounting_during_existing_failover(tmp_path, max_calls):
    from auto_agents.control import Store
    from auto_agents.control.engine import Engine
    from test_control_engine import Worker, project

    root = project(tmp_path)
    config = ProjectConfig("fixture")
    config.providers["codex-fuli0110"] = ProviderConfig(kind="mock", binary="fixture")

    class QuotaThenWorker(Provider):
        def __init__(self):
            super().__init__(config)
            self.worker, self.aliases = Worker(), []

        def adapter(self, alias):
            return SimpleNamespace(available=lambda: True, run=lambda request: AgentResult(
                False, ["fixture"], request.output_path, stderr=QUOTA,
                termination=AgentTermination(reason="provider_error"),
            ))

        def run(self, ctx, phase, *args, **kwargs):
            self.aliases.append(ctx.provider)
            if ctx.provider == "codex-fuli0110":
                return super().run(ctx, phase, *args, **kwargs)
            return self.worker.run(ctx, phase, *args, **kwargs)

    transport = QuotaThenWorker()
    engine = Engine(root, Store(root), config, provider="codex-fuli0110", transport=transport,
                    print_fn=lambda *args: None)
    work = engine.start("fix", "Set value", max_calls=max_calls)
    assert (work["status"] == "COMPLETED") == (max_calls == 12), work["failure"]
    if max_calls == 1:
        assert work["failure"]["category"] == "budget"
    operations = engine.store.operations(work["id"])
    assert operations[0]["state"] == "FAILED" and operations[0]["consumed"]
    assert operations[0]["result"]["code"] == "provider_quota"
    assert not any(op["state"] in {"UNKNOWN", "DISPATCHED"} for op in operations)
    assert engine.store.workflow(work["workflow"])["calls"] == len(transport.aliases)
    assert engine.store.workflow(work["workflow"])["max_calls"] == max_calls
    assert engine.store.work(work["id"])["max_calls"] == 15
