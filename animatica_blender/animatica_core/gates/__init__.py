"""The half of an acceptance suite that is not about any one DCC.

A gate suite has two parts, and only one of them is host-specific. Deciding
which gates exist, whether the server is up, what a gate's last line means,
and how to print the table is the same work in every host. Actually *starting*
a gate is not: 3ds Max spawns ``3dsmaxbatch``, MotionBuilder submits to the
console of a running application, and the next host will differ again.

So the launcher is a callable this package is given, not something it knows.
See :mod:`animatica_core.gates.harness`.

These modules are plain CPython -- no Qt, no DCC SDK -- so the harness itself
is unit-testable in CI, which is where its parsing has always belonged.

The gates themselves are NOT pytest files (they are named ``accept_*`` /
``gate_*`` / ``smoke_*``), so pytest does not collect them. That is deliberate:
every one of them needs a DCC that CI does not have.
"""
