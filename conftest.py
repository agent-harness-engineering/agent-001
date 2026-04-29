"""Root conftest.py — pytest collection configuration.

The ``certs`` directory is a symlink to a volume that may not be mounted
in every environment (CI, developer machines without the external drive).
Exclude it from collection to prevent PermissionError during test discovery.
"""

collect_ignore = ["certs"]
