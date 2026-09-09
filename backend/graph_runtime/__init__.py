"""Executable agent workflow Graph (Phase 1, frozen slice).

The Graph is an executable, observable, resumable agent workflow network.
The execution and control planes live here; the Loop V1 cognitive knowledge
projection is only a read-only memory input. This package adds no graph
database product, event bus, dispatcher, daemon, or third-party dependency:
every run is a controlled use of the existing SQLite checkpoint store.
"""
