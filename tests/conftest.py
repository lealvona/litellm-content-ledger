import sys
import types


def _install_fake_litellm() -> None:
    """proxy_callback imports LiteLLM's CustomLogger; the tests only need the base class."""
    if "litellm.integrations.custom_logger" in sys.modules:
        return

    class CustomLogger:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    custom_logger = types.ModuleType("litellm.integrations.custom_logger")
    custom_logger.CustomLogger = CustomLogger
    integrations = types.ModuleType("litellm.integrations")
    integrations.custom_logger = custom_logger
    litellm = types.ModuleType("litellm")
    litellm.integrations = integrations
    sys.modules.setdefault("litellm", litellm)
    sys.modules.setdefault("litellm.integrations", integrations)
    sys.modules["litellm.integrations.custom_logger"] = custom_logger


_install_fake_litellm()
