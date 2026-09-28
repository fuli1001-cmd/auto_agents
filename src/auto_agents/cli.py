"""Compatibility entrypoint; select a runtime before importing business code."""
import importlib
import sys
import types


def _implementation():
    return importlib.import_module('.cli_impl', __package__)


def main(argv=None):
    from .bootstrap import main as bootstrap_main
    return bootstrap_main(argv)


class _CompatibilityModule(types.ModuleType):
    # Existing embedders/tests import CLI helpers and patch CLI dependencies.
    # Forward those accesses to the implementation without eagerly importing it
    # in the installed legacy `from auto_agents.cli import main` launcher.
    def __getattr__(self, name):
        if name.startswith('__'): raise AttributeError(name)
        return getattr(_implementation(), name)

    def __setattr__(self, name, value):
        if name in self.__dict__ or name.startswith('__'):
            return super().__setattr__(name, value)
        setattr(_implementation(), name, value)

    def __delattr__(self, name):
        if name in self.__dict__: return super().__delattr__(name)
        delattr(_implementation(), name)


sys.modules[__name__].__class__ = _CompatibilityModule
