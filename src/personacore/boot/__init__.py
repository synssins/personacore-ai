"""The pieces the core assembles, one concern to a module — ADR-0040.

``personacore.server`` wires. It reads settings, builds what is in here, hands
the pieces to each other and starts. Everything that decides *how* one of those
pieces behaves lives in this package instead, so that changing the way speech
starts cannot stop the container importing.

Nothing in here imports ``personacore.server``. The dependency runs one way —
the boot modules sit below the assembly exactly as the domain packages do — and
that is what keeps the assembly free to import all of them.
"""
