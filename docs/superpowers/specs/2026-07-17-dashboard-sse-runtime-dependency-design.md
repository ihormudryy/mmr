# Dashboard SSE Runtime Dependency Repair

## Problem

The command-center dashboard imports `sse_starlette.sse.EventSourceResponse`,
but the runtime Docker image does not install `sse-starlette`. The dependency is
already pinned to `2.1.3` in `pyproject.toml` and `uv.lock`; it is absent from
`requirements.txt`, which is the dependency source installed by the runtime
Dockerfile before the project is installed with `--no-deps`.

## Design

Keep the existing Docker dependency model and add `sse-starlette==2.1.3` to
`requirements.txt`. Add a focused dependency-parity test that verifies every
direct runtime dependency declared by `pyproject.toml` is represented in
`requirements.txt`. This prevents a future dependency from working in the host
`uv` environment while being absent from the production image.

Do not add a conditional SSE fallback and do not change the Docker build to use
`uv.lock`; either would expand a one-package packaging defect into an unrelated
runtime or build-system redesign.

## Verification

1. Demonstrate that the parity test fails before adding the missing requirement.
2. Add the pinned requirement and verify the parity test passes.
3. Rebuild the shared runtime image and recreate the dashboard service.
4. Verify `sse_starlette` imports inside the rebuilt image.
5. Verify the dashboard container remains running and its HTTP health endpoint
   responds successfully.

