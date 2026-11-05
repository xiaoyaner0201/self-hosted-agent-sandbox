"""self-hosted-agent-sandbox: disposable gVisor sandboxes for AI coding agents on your own k3s."""
from .core import Sandbox

__all__ = ["Sandbox", "__version__"]
__version__ = "0.2.0"
