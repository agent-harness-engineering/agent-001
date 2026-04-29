"""
gates — Permission Gate package for the Maestro Agent.

Public surface::

    from gates import PermissionGate, PermissionPolicy, PermissionDecision, PolicyRule
"""

from .policy import PermissionDecision, PermissionPolicy, PolicyRule
from .permission_gate import PermissionGate

__all__ = [
    "PermissionGate",
    "PermissionPolicy",
    "PermissionDecision",
    "PolicyRule",
]
