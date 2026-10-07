"""SP1 acceptance harness (Plan 6): one scenario, two drivers.

The scenario talks only to an ``AcceptancePort`` (typed RPC calls). In tests the
port reaches a composed stack served over loopback sockets; on the host it
reaches the published trader ports. Nothing here opens a database file.
"""
