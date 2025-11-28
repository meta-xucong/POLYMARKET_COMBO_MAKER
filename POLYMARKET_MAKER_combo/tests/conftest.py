import sys
import types
from pathlib import Path

# Provide lightweight stubs when `py_clob_client` is unavailable so import-only
# helper scripts can be collected by pytest without optional dependencies.
if "py_clob_client" not in sys.modules:  # pragma: no cover - test helper
    clob_pkg = types.ModuleType("py_clob_client")

    class _PolyApiException(Exception):
        pass

    class _DummyClobClient:
        def __init__(self, *_, **__):
            self._api_creds = None

        def create_or_derive_api_creds(self):
            return types.SimpleNamespace(key="dummy", secret="secret", passphrase="phrase")

        def set_api_creds(self, creds):
            self._api_creds = creds

        def get_ok(self):
            return {"status": "ok"}

        def get_server_time(self):
            return 0

        def get_orders(self, *_args, **_kwargs):
            return []

    clob_types = types.ModuleType("py_clob_client.clob_types")
    clob_types.OpenOrderParams = lambda *args, **kwargs: (args, kwargs)
    clob_types.OrderArgs = object
    clob_types.OrderType = types.SimpleNamespace(FAK="FAK", GTC="GTC")

    constants = types.ModuleType("py_clob_client.constants")
    constants.POLYGON = 137

    exceptions = types.ModuleType("py_clob_client.exceptions")
    exceptions.PolyApiException = _PolyApiException

    order_builder = types.ModuleType("py_clob_client.order_builder.constants")
    order_builder.BUY = "BUY"
    order_builder.SELL = "SELL"

    clob_pkg.client = types.SimpleNamespace(ClobClient=_DummyClobClient)
    clob_pkg.clob_types = clob_types
    clob_pkg.constants = constants
    clob_pkg.exceptions = exceptions
    clob_pkg.order_builder = types.SimpleNamespace(constants=order_builder)

    sys.modules["py_clob_client"] = clob_pkg
    sys.modules["py_clob_client.client"] = clob_pkg.client
    sys.modules["py_clob_client.clob_types"] = clob_types
    sys.modules["py_clob_client.constants"] = constants
    sys.modules["py_clob_client.exceptions"] = exceptions
    sys.modules["py_clob_client.order_builder.constants"] = order_builder

# Ensure project root is on sys.path so direct imports work when tests are run from repo root
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT_STR = str(PROJECT_ROOT)
if PROJECT_ROOT_STR not in sys.path:
    sys.path.insert(0, PROJECT_ROOT_STR)
