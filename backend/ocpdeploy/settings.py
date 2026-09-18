import os
from pathlib import Path

ROOT = Path(os.environ.get("OCPDEPLOY_ROOT", "/opt/ocpdeploy")).resolve()
CLUSTERS_DIR = ROOT / "clusters"
BIN_DIR = ROOT / "bin"
SECRET_KEY_FILE = Path(os.environ.get("OCPDEPLOY_SECRET_KEY_FILE", ROOT / ".secret_key"))
USERS_FILE = Path(os.environ.get("OCPDEPLOY_USERS_FILE", ROOT / "users.json"))
# imported (connected) clusters live only in RAM: a tmpfs directory wiped at every start
IMPORTED_DIR = Path(os.environ.get("OCPDEPLOY_IMPORTED_DIR", "/dev/shm/ocpdeploy-imported"))
STATIC_DIR = Path(os.environ.get("OCPDEPLOY_STATIC_DIR", Path(__file__).parent / "static"))
TEMPLATES_DIR = Path(__file__).parent / "templates"
LISTEN_HOST = os.environ.get("OCPDEPLOY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("OCPDEPLOY_PORT", "8080"))
MIRROR = "https://mirror.openshift.com/pub/openshift-v4/x86_64/clients/ocp"
GRAPH_URL = "https://api.openshift.com/api/upgrades_info/v1/graph"
# oldest minor the app will offer; IPI static IPs + user-managed LB need 4.15+
MIN_MINOR = 15
DEFAULT_SSH_PUBKEY = Path.home() / ".ssh" / "id_ed25519.pub"
DEFAULT_SSH_KEY = Path.home() / ".ssh" / "id_ed25519"
# URL under which BMCs / hypervisors can fetch ISOs from this app; auto-detected per target when empty
ADVERTISE_URL = os.environ.get("OCPDEPLOY_ADVERTISE_URL", "")

for d in (CLUSTERS_DIR, BIN_DIR, ROOT / "templates"):
    d.mkdir(parents=True, exist_ok=True)
