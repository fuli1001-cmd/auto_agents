import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.models import AgentRequest, AgentResult
from auto_agents.orchestrator import Orchestrator, _FAILOVER_PATTERN


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(ok=True, returncode=0, stderr="", summary="done"):
    return AgentResult(
        ok=ok,
        command=["fake"],
        output_path=Path("/tmp/out"),
        summary=summary,
        stderr=stderr,
        returncode=returncode,
    )


def _make_request():
    return AgentRequest(
        stage="plan",
        effort="balanced",
        prompt="test prompt",
        cwd=Path("/tmp"),
        output_path=Path("/tmp/out"),
    )


class _FakeAdapter:
    """Adapter that returns a pre-configured result and records calls."""

    def __init__(self, result: AgentResult, is_available: bool = True):
        self._result = result
        self._is_available = is_available
        self.calls = 0

    def available(self) -> bool:
        return self._is_available

    def run(self, request: AgentRequest) -> AgentResult:
        self.calls += 1
        return self._result


class _SequenceAdapter:
    """Adapter that returns different results on successive calls."""

    def __init__(self, results, is_available=True):
        self._results = list(results)
        self._is_available = is_available
        self.calls = 0

    def available(self):
        return self._is_available

    def run(self, request):
        result = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        return result


# ---------------------------------------------------------------------------
# Minimal Orchestrator stub for unit-testing failover methods in isolation
# ---------------------------------------------------------------------------


def _stub_orchestrator(providers_dict, active_provider, adapters_map):
    """Create a lightweight Orchestrator-like object for failover testing.

    ``adapters_map`` maps provider kind → adapter instance.
    """

    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.providers = providers_dict
    cfg.active_provider = active_provider

    class _Stub(Orchestrator):
        def __new__(cls):
            return object.__new__(cls)

        def __init__(self):
            pass

    stub = _Stub()
    stub.config = cfg
    stub.adapter = adapters_map.get(active_provider)
    stub.agent_output_stream = io.StringIO()
    stub._last_successful_provider = None
    stub._failed_providers = set()
    stub._adapters_map = adapters_map

    # Override _build_adapter_for_provider to use the test adapters_map
    def _build_adapter_for_provider(kind):
        return adapters_map[kind]

    stub._build_adapter_for_provider = _build_adapter_for_provider
    return stub


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIsFailoverError(unittest.TestCase):
    def test_ok_result_not_failover(self):
        r = _make_result(ok=True, returncode=0, stderr="rate limit hit")
        self.assertFalse(Orchestrator._is_failover_error(r))

    def test_non_qualifying_error(self):
        r = _make_result(ok=False, returncode=1, stderr="syntax error on line 42")
        self.assertFalse(Orchestrator._is_failover_error(r))

    def test_rate_limit(self):
        r = _make_result(ok=False, returncode=1, stderr="Error: rate limit exceeded")
        self.assertTrue(Orchestrator._is_failover_error(r))

    def test_429(self):
        r = _make_result(ok=False, returncode=1, stderr="HTTP 429 Too Many Requests")
        self.assertTrue(Orchestrator._is_failover_error(r))

    def test_quota(self):
        r = _make_result(ok=False, returncode=1, stderr="API quota exhausted")
        self.assertTrue(Orchestrator._is_failover_error(r))

    def test_unavailable(self):
        r = _make_result(ok=False, returncode=1, stderr="service unavailable")
        self.assertTrue(Orchestrator._is_failover_error(r))

    def test_not_found_binary(self):
        r = _make_result(ok=False, returncode=127, stderr="codex: not found")
        self.assertTrue(Orchestrator._is_failover_error(r))

    def test_enoent(self):
        r = _make_result(ok=False, returncode=1, stderr="ENOENT: no such file")
        self.assertTrue(Orchestrator._is_failover_error(r))

    def test_capacity(self):
        r = _make_result(ok=False, returncode=1, stderr="No capacity available for model gpt-4")
        self.assertTrue(Orchestrator._is_failover_error(r))

    def test_empty_stderr_not_failover(self):
        r = _make_result(ok=False, returncode=1, stderr="")
        self.assertFalse(Orchestrator._is_failover_error(r))


class TestFailoverProviderOrder(unittest.TestCase):
    def test_active_first(self):
        stub = _stub_orchestrator(
            {"codex": {}, "copilot-cli": {}}, "codex", {}
        )
        self.assertEqual(stub._failover_provider_order(), ["codex", "copilot-cli"])

    def test_active_first_reversed(self):
        stub = _stub_orchestrator(
            {"codex": {}, "copilot-cli": {}}, "copilot-cli", {}
        )
        self.assertEqual(stub._failover_provider_order(), ["copilot-cli", "codex"])


class TestCallWithFailover(unittest.TestCase):
    def test_active_succeeds_no_failover(self):
        ok_result = _make_result(ok=True)
        codex = _FakeAdapter(ok_result)
        copilot = _FakeAdapter(_make_result(ok=True))
        stub = _stub_orchestrator(
            {"codex": {}, "copilot-cli": {}}, "codex",
            {"codex": codex, "copilot-cli": copilot},
        )
        result = stub._call_with_failover(_make_request())
        self.assertTrue(result.ok)
        self.assertEqual(codex.calls, 1)
        self.assertEqual(copilot.calls, 0)

    def test_switches_on_quota_error(self):
        quota_result = _make_result(ok=False, returncode=1, stderr="429 Too Many Requests")
        ok_result = _make_result(ok=True, summary="from copilot")
        codex = _FakeAdapter(quota_result)
        copilot = _FakeAdapter(ok_result)
        stub = _stub_orchestrator(
            {"codex": {}, "copilot-cli": {}}, "codex",
            {"codex": codex, "copilot-cli": copilot},
        )
        result = stub._call_with_failover(_make_request())
        self.assertTrue(result.ok)
        self.assertEqual(result.summary, "from copilot")
        self.assertEqual(codex.calls, 1)
        self.assertEqual(copilot.calls, 1)

    def test_all_providers_exhausted(self):
        quota_result = _make_result(ok=False, returncode=1, stderr="rate limit exceeded")
        codex = _FakeAdapter(quota_result)
        copilot = _FakeAdapter(quota_result)
        stub = _stub_orchestrator(
            {"codex": {}, "copilot-cli": {}}, "codex",
            {"codex": codex, "copilot-cli": copilot},
        )
        with self.assertRaises(RuntimeError) as ctx:
            stub._call_with_failover(_make_request())
        self.assertIn("All providers exhausted", str(ctx.exception))
        self.assertIn("codex", str(ctx.exception))
        self.assertIn("copilot-cli", str(ctx.exception))

    def test_non_qualifying_error_no_switch(self):
        logic_error = _make_result(ok=False, returncode=1, stderr="invalid JSON at line 5")
        copilot = _FakeAdapter(_make_result(ok=True))
        codex = _FakeAdapter(logic_error)
        stub = _stub_orchestrator(
            {"codex": {}, "copilot-cli": {}}, "codex",
            {"codex": codex, "copilot-cli": copilot},
        )
        result = stub._call_with_failover(_make_request())
        self.assertFalse(result.ok)
        self.assertEqual(codex.calls, 1)
        self.assertEqual(copilot.calls, 0)  # never tried

    def test_single_provider_exhausted(self):
        quota_result = _make_result(ok=False, returncode=1, stderr="quota exhausted")
        codex = _FakeAdapter(quota_result)
        stub = _stub_orchestrator(
            {"codex": {}}, "codex",
            {"codex": codex},
        )
        with self.assertRaises(RuntimeError) as ctx:
            stub._call_with_failover(_make_request())
        self.assertIn("All providers exhausted", str(ctx.exception))

    def test_binary_not_found_skips(self):
        ok_result = _make_result(ok=True, summary="copilot ok")
        codex = _FakeAdapter(_make_result(), is_available=False)
        copilot = _FakeAdapter(ok_result)
        stub = _stub_orchestrator(
            {"codex": {}, "copilot-cli": {}}, "codex",
            {"codex": codex, "copilot-cli": copilot},
        )
        result = stub._call_with_failover(_make_request())
        self.assertTrue(result.ok)
        self.assertEqual(result.summary, "copilot ok")
        self.assertEqual(codex.calls, 0)  # skipped, never called run()
        self.assertEqual(copilot.calls, 1)
        log = stub.agent_output_stream.getvalue()
        self.assertIn("binary not found", log)

    def test_memory_reorders_after_failover(self):
        """After provider B succeeds via failover, next call tries B first."""
        quota_result = _make_result(ok=False, returncode=1, stderr="429 rate limit")
        ok_result = _make_result(ok=True, summary="ok")
        stub = _stub_orchestrator(
            {"codex": {}, "copilot-cli": {}}, "codex",
            {"codex": _FakeAdapter(quota_result), "copilot-cli": _FakeAdapter(ok_result)},
        )
        # First call: codex fails → copilot-cli succeeds
        stub._call_with_failover(_make_request())
        self.assertEqual(stub._last_successful_provider, "copilot-cli")
        self.assertIn("codex", stub._failed_providers)

        # Second call: new adapters — copilot-cli should be tried first
        codex2 = _FakeAdapter(quota_result)
        copilot2 = _FakeAdapter(ok_result)
        stub._adapters_map = {"codex": codex2, "copilot-cli": copilot2}
        stub._build_adapter_for_provider = lambda kind: stub._adapters_map[kind]
        # Need to update self.adapter too since active is codex
        stub.adapter = codex2

        result = stub._call_with_failover(_make_request())
        self.assertTrue(result.ok)
        # copilot-cli was tried first (via memory) and succeeded
        self.assertEqual(copilot2.calls, 1)
        self.assertEqual(codex2.calls, 0)

    def test_failed_provider_deprioritized(self):
        """Provider that failed is tried last in subsequent calls with 3+ providers."""
        quota = _make_result(ok=False, returncode=1, stderr="rate limit")
        ok = _make_result(ok=True)
        # 3 providers: codex (active), copilot-cli, shell
        stub = _stub_orchestrator(
            {"codex": {}, "copilot-cli": {}, "shell": {}}, "codex",
            {
                "codex": _FakeAdapter(quota),
                "copilot-cli": _FakeAdapter(quota),
                "shell": _FakeAdapter(ok),
            },
        )
        # First call: codex fails, copilot-cli fails, shell succeeds
        stub._call_with_failover(_make_request())
        self.assertEqual(stub._last_successful_provider, "shell")
        self.assertIn("codex", stub._failed_providers)
        self.assertIn("copilot-cli", stub._failed_providers)

        # Second call: order should be [shell, ...failed]
        # shell succeeds immediately
        shell2 = _FakeAdapter(ok)
        codex2 = _FakeAdapter(quota)
        copilot2 = _FakeAdapter(quota)
        stub._adapters_map = {"codex": codex2, "copilot-cli": copilot2, "shell": shell2}
        stub._build_adapter_for_provider = lambda kind: stub._adapters_map[kind]
        stub.adapter = codex2

        result = stub._call_with_failover(_make_request())
        self.assertTrue(result.ok)
        self.assertEqual(shell2.calls, 1)
        self.assertEqual(codex2.calls, 0)
        self.assertEqual(copilot2.calls, 0)

    def test_recovered_provider_rejoins(self):
        """A previously failed provider that now succeeds is removed from _failed_providers."""
        quota = _make_result(ok=False, returncode=1, stderr="rate limit")
        ok = _make_result(ok=True)
        stub = _stub_orchestrator(
            {"codex": {}, "copilot-cli": {}}, "codex",
            {"codex": _FakeAdapter(quota), "copilot-cli": _FakeAdapter(ok)},
        )
        # codex fails, copilot succeeds
        stub._call_with_failover(_make_request())
        self.assertIn("codex", stub._failed_providers)

        # Now codex recovers
        codex_ok = _FakeAdapter(ok)
        copilot2 = _FakeAdapter(ok)
        stub._adapters_map = {"codex": codex_ok, "copilot-cli": copilot2}
        stub._build_adapter_for_provider = lambda kind: stub._adapters_map[kind]

        # Simulate: _last_successful_provider is copilot-cli, so it tries copilot-cli first
        # But let's set _last_successful_provider to None to test codex as first (active)
        stub._last_successful_provider = None
        stub.adapter = codex_ok

        result = stub._call_with_failover(_make_request())
        self.assertTrue(result.ok)
        self.assertEqual(codex_ok.calls, 1)
        # codex succeeded → removed from _failed_providers
        self.assertNotIn("codex", stub._failed_providers)

    def test_failover_log_output(self):
        """Verify log messages for failover events."""
        quota = _make_result(ok=False, returncode=1, stderr="rate limit exceeded")
        ok = _make_result(ok=True)
        stub = _stub_orchestrator(
            {"codex": {}, "copilot-cli": {}}, "codex",
            {"codex": _FakeAdapter(quota), "copilot-cli": _FakeAdapter(ok)},
        )
        stub._call_with_failover(_make_request())
        log = stub.agent_output_stream.getvalue()
        self.assertIn("[failover] provider=codex quota/rate error", log)
        self.assertIn("[failover] using provider=copilot-cli", log)


if __name__ == "__main__":
    unittest.main()
