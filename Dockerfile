FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ---------------------------------------------------------------------------
# Governance read-only mount specification (see governance/docker-mount-spec.md)
#
# In production, mount these paths read-only so the agent cannot alter its
# own constraints — even if the process is compromised.  The audit log
# directory must remain read-write (append-only by convention).
#
# Example docker-compose volume stanza:
#   volumes:
#     - ./mission.md:/app/mission.md:ro
#     - ./mind.md:/app/mind.md:ro
#     - ./morals.md:/app/morals.md:ro
#     - ./memory_module.md:/app/memory_module.md:ro
#     - ./gates:/app/gates:ro
#     - ./governance:/app/governance:ro
#     - ./audit_logs:/app/audit_logs:rw   # audit log — rw required
#
# Set read_only: true at the service level and add a tmpfs for /tmp.
# See governance/docker-mount-spec.md for the full Kubernetes equivalent.
# ---------------------------------------------------------------------------

EXPOSE 8095

CMD ["python", "server.py"]
