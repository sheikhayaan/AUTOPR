"""The sandbox package: apply a proposed fix and prove it in isolation.

- policy:   pure — what command verifies each failure type, how to read exits.
- runner:   the Docker execution boundary (DockerSandbox) + a FakeSandbox.
- verifier: the LangGraph node that ties them to PRState.
"""
