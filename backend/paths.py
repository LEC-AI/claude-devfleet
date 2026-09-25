"""Host ↔ container path translation (DEVFLEET_PATH_MAP_* env vars)."""

import os

# Path mapping: host paths ↔ container paths
# e.g. /home/user/my-project → /workspace/my-project (inside Docker)
_PATH_MAPS = []
for env_key, env_val in os.environ.items():
    if env_key.startswith("DEVFLEET_PATH_MAP_"):
        # Format: HOST_PATH:CONTAINER_PATH
        parts = env_val.split(":", 1)
        if len(parts) == 2:
            _PATH_MAPS.append((parts[0], parts[1]))


def resolve_path(path: str) -> str:
    """Translate a host path to a container path if running in Docker."""
    for host_prefix, container_prefix in _PATH_MAPS:
        if path.startswith(host_prefix):
            return path.replace(host_prefix, container_prefix, 1)
    return path


def reverse_path(path: str) -> str:
    """Translate a container path back to a host path for display."""
    for host_prefix, container_prefix in _PATH_MAPS:
        if path.startswith(container_prefix):
            return path.replace(container_prefix, host_prefix, 1)
    return path
