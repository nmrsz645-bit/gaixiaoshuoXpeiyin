import sys
from pathlib import Path


FROZEN = getattr(sys, "frozen", False)
BASE_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent


def configure_certifi():
    import certifi

    candidates = [
        BASE_DIR / "cacert.pem",
        BASE_DIR / "_internal" / "cacert.pem",
        BASE_DIR / "_internal" / "certifi" / "cacert.pem",
        Path(certifi.where()),
    ]
    certificate = next((path for path in candidates if path.is_file()), None)
    if certificate is None:
        raise FileNotFoundError(f"HTTPS certificate not found. Checked: {', '.join(map(str, candidates))}")
    certifi.where = lambda: str(certificate)
    return certificate
