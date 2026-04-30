import os


def get_device() -> str:
    """Auto-detect compute device: cuda > mps > cpu"""
    override = os.getenv("EMBED_DEVICE")
    if override:
        return override
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
    except ImportError:
        pass
    return "cpu"
