"""The group a lock can give the project's own dependencies.

A leaf module, so the builder and the writer can both reach the member
without importing each other.
"""

from __future__ import annotations

from nab_provider.conflict_kind import KIND_GROUP

BASE_MEMBER = (KIND_GROUP, "")
"""Stands for the project's own dependencies in a gate.

The builder records reachability before anything knows whether the lock
will name them, so it records this and the writer substitutes the
configured name or drops the gate. It is never rendered: the empty name
belongs to no group.
"""
