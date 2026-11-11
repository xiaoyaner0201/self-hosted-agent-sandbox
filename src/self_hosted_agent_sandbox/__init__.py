"""self-hosted-agent-sandbox: disposable gVisor sandboxes for AI coding agents on your own k3s."""
from .core import Allocation, ReleaseError, Sandbox

__all__ = ["Allocation", "ReleaseError", "Sandbox", "__version__"]
__version__ = "0.4.0"
