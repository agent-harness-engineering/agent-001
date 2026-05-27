# Docker Read-Only Mount Specification for Maestro Agent Governance Files

## Principle

Governance files are the immutable contract between the operator and the agent.
They must be physically read-only inside the container so that even a compromised
or misbehaving agent process cannot alter its own constraints at runtime.

The audit log directory is the one exception: it must be read-write (append-only
by convention) so the audit trail can be written to durable storage.

---

## Protected Paths (read-only inside container)

| Host path (relative to repo root)  | Container path            | Mount mode |
|------------------------------------|---------------------------|------------|
| `./mission.md`                     | `/app/mission.md`         | `ro`       |
| `./mind.md`                        | `/app/mind.md`            | `ro`       |
| `./morals.md`                      | `/app/morals.md`          | `ro`       |
| `./memory_module.md`               | `/app/memory_module.md`   | `ro`       |
| `./gates/`                         | `/app/gates/`             | `ro`       |
| `./governance/`                    | `/app/governance/`        | `ro`       |

## Audit Log Directory (read-write, append-only by convention)

| Host path               | Container path  | Mount mode |
|-------------------------|-----------------|------------|
| `./audit_logs/`         | `/app/audit_logs/` | `rw`    |

The host directory `./audit_logs/` should be owned by the same UID the container
process runs as, and should **not** be world-writable.

---

## docker-compose Example

```yaml
version: "3.9"

services:
  agent-001:
    build: .
    image: agent-001:latest
    ports:
      - "8088:8088"
    env_file:
      - .env
    volumes:
      # --- Governance: read-only ---
      - ./mission.md:/app/mission.md:ro
      - ./mind.md:/app/mind.md:ro
      - ./morals.md:/app/morals.md:ro
      - ./memory_module.md:/app/memory_module.md:ro
      - ./gates:/app/gates:ro
      - ./governance:/app/governance:ro

      # --- Audit log: read-write (append-only by convention) ---
      - ./audit_logs:/app/audit_logs:rw

    # Drop all Linux capabilities the process does not need.
    # read_only: true prevents writes to the container filesystem layer
    # itself (except for explicitly mounted rw volumes above).
    read_only: true
    tmpfs:
      - /tmp           # scratch space for temporary files
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    restart: unless-stopped
```

---

## Runtime Verification

On startup the agent calls `GovernanceGuard.verify_read_only_mounts()` to
confirm that protected paths are actually non-writable by the running process:

```python
from pathlib import Path
from governance.self_exemption import GovernanceGuard

guard = GovernanceGuard(repo_root=Path("/app"))
status = GovernanceGuard.verify_read_only_mounts(guard.get_protected_paths())
for path, is_ro in status.items():
    if not is_ro:
        raise RuntimeError(f"Governance path is writable — mount misconfigured: {path}")
```

If any protected path is writable, the agent should refuse to start (or emit
a high-severity alert and continue in degraded mode, depending on operator policy).

---

## Notes

- `read_only: true` at the service level means the container's root filesystem
  is read-only. Combined with individual `rw` mounts for `audit_logs/` and
  `tmpfs` for `/tmp`, the agent has exactly the write surface it needs and
  nothing more.
- The `governance/` directory is mounted read-only even though it contains
  Python source code. Updates to governance logic require an image rebuild and
  re-deployment — intentional friction that prevents in-place tampering.
- If using Kubernetes, replace the `volumes` / `volumeMounts` pattern with
  `ConfigMap` or `Secret` mounts using `readOnly: true`, and a `PersistentVolumeClaim`
  for the audit log with appropriate RBAC.
