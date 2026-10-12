"""The sandbox full-stack runner shares nothing with a live stack on the same host (SP3 Plan 2 ruling 30)."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.compose_rpc_helpers import load_compose, load_override, volumes

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "run_sandbox_fullstack.sh"
SBXTEST_OVERRIDE = ROOT / "docker-compose.sbxtest.override.yml"
sys.path.insert(0, str(ROOT / "scripts"))
from check_sbxtest_config import LIVE_PORTS, PRODUCTION_IMAGE, problems  # noqa: E402

HOME = "/Users/operator"
IMAGE = "mmr-sbxtest:abc123-test"
SAFE = {"name": "mmr-sbxtest",
        "services": {"sandbox": {"image": IMAGE, "build": {"context": "."},
                                 "volumes": [{"type": "volume", "source": "mmr_sandbox_live"}]},
                     "fullstack-tests": {"image": IMAGE + "-test", "build": {"context": "."},
                                         "environment": {"MMR_FAKE_BROKER": "1", "TZ": "UTC"},
                                         "volumes": [{"type": "bind", "source": "/var/run/docker.sock"}]}},
        "volumes": {"mmr_sandbox_live": {"name": "mmr-sbxtest_mmr_sandbox_live"}}}


def test_a_safe_config_passes():
    assert problems(SAFE, operator_home=HOME, repo_root="/repo") == []


@pytest.mark.parametrize("change, fragment", [
    (lambda c: c.update(name="mmr"), "project 'mmr'"),
    (lambda c: c["services"].update({"ib-gateway": {"image": "ghcr.io/x:latest"}}), "needs no broker gateway"),
    (lambda c: c["services"].update({"trader": {"image": IMAGE + "-trader"}}), "not one of"),
    (lambda c: c["services"]["sandbox"].update(image=PRODUCTION_IMAGE), "production image"),
    (lambda c: c["services"]["sandbox"].update(image="mmr:some-other"), "not a mmr-sbxtest"),
    (lambda c: c["services"]["fullstack-tests"].update(image=IMAGE), "both build image"),  # == sandbox's tag
    (lambda c: c["services"]["sandbox"].update(environment={"TWS_USERID": "live-user"}), "credential/mode"),
    (lambda c: c["services"]["sandbox"].update(environment={"TRADING_MODE": "live"}), "credential/mode"),
    (lambda c: c["services"]["sandbox"].update(environment={"IB_ACCOUNT": "U123"}), "credential/mode"),
    (lambda c: c["services"]["sandbox"].update(environment={"ALPACA_API_KEY_ID": "k"}), "credential/mode"),
    (lambda c: c["services"]["sandbox"].update(environment={"OPENROUTER_API_KEY": "k"}), "credential/mode"),
    (lambda c: c["services"]["sandbox"].update(ports=[{"published": "42101", "target": 42101}]), "a live port"),
    (lambda c: c["services"]["sandbox"].update(ports=[{"published": "55555", "target": 1}]), "publishes host port"),
    (lambda c: c["services"]["sandbox"]["volumes"].append({"type": "bind", "source": f"{HOME}/.config/mmr"}),
     "live config"),
    (lambda c: c["services"]["sandbox"]["volumes"].append(
        {"type": "bind", "source": f"{HOME}/.config/mmr/keys/rpc/trader.key"}), "live config"),
    (lambda c: c["services"]["sandbox"]["volumes"].append(
        {"type": "bind", "source": f"{HOME}/.local/share/mmr/logs"}), "live config"),
    (lambda c: c["services"]["sandbox"].update(env_file=[{"path": "/repo/.env"}]), "live credentials"),
    (lambda c: c["services"]["sandbox"].update(env_file=[{"path": "/elsewhere/ib.env"}]), "live credentials"),
    (lambda c: c["volumes"].update(mmr_ai_data={"name": "mmr_mmr_ai_data"}), "outside the test project"),
    (lambda c: c["services"]["sandbox"].update(network_mode="host"), "host network"),
    (lambda c: c["services"]["sandbox"].update(privileged=True), "privileged"),
    (lambda c: c["services"]["sandbox"].update(pid="host"), "host pid"),
    (lambda c: c["services"]["sandbox"].update(ipc="host"), "host ipc"),
    (lambda c: c.update(networks={"mmr-internal": {"name": "mmr_mmr-internal"}}), "network mmr-internal"),
    (lambda c: c.update(networks={"shared": {"name": "mmr-sbxtest_shared", "external": True}}), "external"),
    (lambda c: c.update(configs={"cfg": {"file": f"{HOME}/.config/mmr/trader.yaml"}}), "config cfg"),
    (lambda c: c.update(secrets={"key": {"file": "/somewhere/ib.key"}}), "secret key"),
    (lambda c: c["services"]["sandbox"].update(environment={"ANTHROPIC_API_KEY": "k"}), "credential/mode"),
    (lambda c: c["services"]["sandbox"].update(environment={"DASHBOARD_PASSWORD": "k"}), "credential/mode"),
    (lambda c: c["services"]["sandbox"].update(environment={"MMR_WEB_TOKEN": "k"}), "credential/mode"),
    (lambda c: c["services"]["sandbox"].update(environment={"telegram_bot_token": "k"}), "credential/mode"),
])
def test_each_way_to_touch_a_live_stack_is_refused(change, fragment):
    config = json.loads(json.dumps(SAFE))
    change(config)
    assert any(fragment in line for line in problems(config, operator_home=HOME, repo_root="/repo"))


def test_an_empty_credential_value_is_allowed():
    config = json.loads(json.dumps(SAFE))
    config["services"]["sandbox"]["environment"] = {"TWS_USERID": "", "TRADING_MODE": ""}
    assert problems(config, operator_home=HOME, repo_root="/repo") == []


def test_a_list_style_environment_is_inspected_too():
    config = json.loads(json.dumps(SAFE))
    config["services"]["sandbox"]["environment"] = ["TWS_PASSWORD=secret"]
    assert any("credential/mode" in line for line in problems(config, operator_home=HOME, repo_root="/repo"))


def test_the_checker_refuses_every_name_the_sandbox_posture_forbids():
    # One list: whatever the running sandbox reports as a forbidden env name, the runner refuses to inject.
    from trader.sandbox.posture import FORBIDDEN_ENV_NAMES, FORBIDDEN_ENV_PREFIXES

    from check_sbxtest_config import CREDENTIAL_NAMES, CREDENTIAL_PREFIXES  # noqa: E402

    assert set(FORBIDDEN_ENV_PREFIXES) <= set(CREDENTIAL_PREFIXES)
    assert FORBIDDEN_ENV_NAMES <= CREDENTIAL_NAMES and "TRADING_MODE" in CREDENTIAL_NAMES


def test_the_live_port_list_covers_the_gateway_rpc_and_dashboard_ports():
    assert {7496, 7497, 5901, 7424, 8081, 42001, 42005, 42101, 42102, 42104, 42105} <= LIVE_PORTS


def test_the_checker_reads_stdin_and_exits_2_on_a_problem():
    unsafe = json.loads(json.dumps(SAFE))
    unsafe["name"] = "mmr"
    script = ROOT / "scripts" / "check_sbxtest_config.py"
    refused = subprocess.run([sys.executable, str(script), HOME, "/repo"], input=json.dumps(unsafe),
                             capture_output=True, text=True, timeout=30)
    passed = subprocess.run([sys.executable, str(script), HOME, "/repo"], input=json.dumps(SAFE),
                            capture_output=True, text=True, timeout=30)
    assert refused.returncode == 2 and "refusing: project 'mmr'" in refused.stderr
    assert passed.returncode == 0 and passed.stderr == ""


def test_the_override_pins_the_two_services_to_test_tags_and_strips_live_ports():
    services = load_override(SBXTEST_OVERRIDE.name)["services"]
    assert services["sandbox"]["image"] == "${MMR_SBXTEST_IMAGE}"
    assert services["fullstack-tests"]["image"] == "${MMR_SBXTEST_IMAGE}-test"
    command = services["fullstack-tests"]["command"]
    assert "tests/fullstack/test_sandbox_service.py" in command and "--timeout=600" in command
    assert "-rA" in command                                       # every outcome is listed, failures included
    environment = services["fullstack-tests"]["environment"]
    assert "TRADING_MODE" not in environment
    # Without these the module would skip everything and pytest would exit 0 (review): pin them.
    assert environment["MMR_FAKE_BROKER"] == "1" and environment["MMR_SBXTEST_STRICT"] == "1"
    for name in ("ib-gateway", "trader", "dashboard"):
        assert services[name]["ports"] == []


PASSED_SUMMARY = "============================== 34 passed in 13.48s =============================="


def run_with_fake_docker(tmp_path, *, run_status: int = 0, run_output: str = PASSED_SUMMARY, **env):
    bin_dir, log = tmp_path / "bin", tmp_path / "docker.log"
    bin_dir.mkdir()
    rendered = json.dumps(SAFE)
    # `docker` logs its whole environment and argv, so a test can prove env -i stripped the operator's variables.
    # `run` (the pytest container) prints ``run_output`` and exits with ``run_status``; `logs` prints a marker.
    (bin_dir / "docker").write_text(
        f'#!/bin/bash\n{{ echo "ENV:"; env; echo "ARGV: $*"; }} >> {log}\n'
        f'if [[ " $* " == *" config "* ]]; then echo \'{rendered}\'; fi\n'
        f'if [[ " $* " == *" logs "* ]]; then echo SANDBOX-LOG-LINES; fi\n'
        f'if [[ " $* " == *" run "* ]]; then echo {json.dumps(run_output)}; exit {run_status}; fi\n')
    (bin_dir / "docker").chmod(0o755)
    (bin_dir / "uv").write_text(f'#!/bin/bash\n{{ echo "ENV:"; env; echo "ARGV: uv HOME=$HOME $*"; }} >> {log}\n')
    (bin_dir / "uv").chmod(0o755)
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=60,
                            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": HOME, **env})
    return result, (log.read_text().splitlines() if log.exists() else [])


def _argv(lines):
    return [line[len("ARGV: "):] for line in lines if line.startswith("ARGV: ")]


def test_the_runner_uses_a_throwaway_home_the_test_project_and_only_the_two_services(tmp_path):
    result, lines = run_with_fake_docker(tmp_path)
    assert result.returncode == 0, result.stderr
    argv = _argv(lines)
    compose = [call for call in argv if call.startswith("compose -p mmr-sbxtest ")]
    assert compose and all("docker-compose.sbxtest.override.yml" in call and "--env-file" in call for call in compose)
    assert all(not call.startswith(f"uv HOME={HOME} ") for call in argv)         # never the operator's home
    keys = next(call for call in argv if "keys init" in call)                    # test-only keys
    assert "--keys-dir" in keys and f"{HOME}/" not in keys
    build = next(call for call in compose if " build " in f" {call} ")
    assert build.rstrip().endswith("build sandbox fullstack-tests")              # only the two services
    up = next(call for call in compose if " up " in f" {call} ")
    # Detached: no compose watcher, so the restart test's sandbox exit cannot abort the run (phase 2, run 1).
    assert " -d " in f" {up} " and "--wait" in up and up.rstrip().endswith(" sandbox")
    run = next(call for call in compose if " run " in f" {call} ")
    assert "--rm" in run and "--no-deps" in run and run.rstrip().endswith(" fullstack-tests")
    assert "--service-ports" not in run                                          # still no published port
    assert compose.index(build) < compose.index(up) < compose.index(run)
    assert "ib-gateway" not in " ".join(compose) and "trader" not in up + run
    assert any(" down -v " in f" {call} " for call in compose)


def test_a_failed_test_run_returns_its_status_and_shows_the_sandbox_logs(tmp_path):
    result, lines = run_with_fake_docker(tmp_path, run_status=1)
    assert result.returncode == 1
    assert "SANDBOX-LOG-LINES" in result.stdout + result.stderr
    assert any(" down -v " in f" {call} " for call in _argv(lines))              # cleanup still runs


@pytest.mark.parametrize("summary", [
    "======================== 30 passed, 4 skipped in 9.00s =========================",
    "============================= 34 skipped in 0.10s ==============================",
    "============================ no tests ran in 0.01s =============================",
    "=================== 33 passed, 1 deselected in 9.00s ===================",
    "",
])
def test_a_run_that_skipped_or_ran_nothing_is_a_failure_even_when_pytest_exits_0(tmp_path, summary):
    result, lines = run_with_fake_docker(tmp_path, run_output=summary)
    assert result.returncode != 0
    assert "every test must pass" in result.stderr
    assert any(" down -v " in f" {call} " for call in _argv(lines))


def test_a_long_run_summary_with_its_clock_time_still_passes(tmp_path):
    result, _ = run_with_fake_docker(tmp_path, run_output="=========== 34 passed in 75.32s (0:01:15) ===========")
    assert result.returncode == 0, result.stderr


def test_cleanup_removes_only_the_runs_own_test_images(tmp_path):
    result, lines = run_with_fake_docker(tmp_path, MMR_BUILD_ID="abc123-test")
    assert result.returncode == 0, result.stderr
    removals = [call for call in _argv(lines) if call.startswith("image rm ")]
    assert removals == ["image rm mmr-sbxtest:abc123-test mmr-sbxtest:abc123-test-test"]


def test_a_passing_test_run_returns_0_without_the_sandbox_logs(tmp_path):
    result, _ = run_with_fake_docker(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "SANDBOX-LOG-LINES" not in result.stdout + result.stderr


def test_the_throwaway_home_is_removed_when_the_run_ends(tmp_path):
    result, lines = run_with_fake_docker(tmp_path)
    assert result.returncode == 0, result.stderr
    homes = {line.split("=", 1)[1] for line in lines if line.startswith("HOME=")}
    assert homes and HOME not in homes
    assert all(not Path(home).exists() for home in homes)


def test_compose_runs_without_the_operator_broker_variables(tmp_path):
    result, lines = run_with_fake_docker(
        tmp_path, TWS_USERID="live-user", TWS_PASSWORD="secret", TRADING_MODE="live", IB_ACCOUNT="U999",
        ALPACA_API_KEY_ID="ak", OPENROUTER_API_KEY="ok")
    assert result.returncode == 0, result.stderr
    env_block = "\n".join(lines)
    for leaked in ("live-user", "secret", "TRADING_MODE=live", "U999", "ALPACA_API_KEY_ID", "OPENROUTER_API_KEY"):
        assert leaked not in env_block, leaked                      # env -i kept them out of every docker/uv call


def test_only_the_daemon_selection_variables_pass_through(tmp_path):
    result, lines = run_with_fake_docker(tmp_path, DOCKER_HOST="unix:///tmp/other.sock",
                                         DOCKER_CONTEXT="desktop-linux", SOME_OPERATOR_VAR="x")
    assert result.returncode == 0, result.stderr
    assert "DOCKER_HOST=unix:///tmp/other.sock" in lines and "DOCKER_CONTEXT=desktop-linux" in lines
    assert not any(line.startswith("SOME_OPERATOR_VAR=") for line in lines)


def test_a_rendered_config_that_touches_a_live_stack_stops_the_run_before_up(tmp_path):
    unsafe = json.loads(json.dumps(SAFE))
    unsafe["services"]["sandbox"]["ports"] = [{"published": "42101", "target": 42101}]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    (bin_dir / "docker").write_text(
        f'#!/bin/bash\necho "$*" >> {log}\n'
        f'if [[ " $* " == *" config "* ]]; then echo \'{json.dumps(unsafe)}\'; fi\n')
    (bin_dir / "docker").chmod(0o755)
    (bin_dir / "uv").write_text("#!/bin/bash\nexit 0\n")
    (bin_dir / "uv").chmod(0o755)
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=60,
                            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": HOME})
    assert result.returncode == 2 and "a live port" in result.stderr
    assert not any(" up " in f" {line} " for line in log.read_text().splitlines())


@pytest.mark.parametrize("project", ["mmr", "prod", ""])
def test_a_project_that_could_be_the_live_stack_is_refused(tmp_path, project):
    result, calls = run_with_fake_docker(tmp_path, MMR_SBXTEST_PROJECT=project)
    assert result.returncode == 2 and calls == []
    assert "refusing" in result.stderr


def test_the_script_warns_against_a_live_host():
    assert "never run this against the live stack" in SCRIPT.read_text().lower()


def test_both_scripts_are_executable():
    assert os.access(SCRIPT, os.X_OK)
    assert os.access(ROOT / "scripts" / "check_sbxtest_config.py", os.X_OK)


def _render(tmp_path):
    empty_env = tmp_path / "compose.env"
    empty_env.write_text("")
    # Exactly what the runner renders: the two services, a test-only image, and a clean environment.
    return subprocess.run(
        ["docker", "compose", "-p", "mmr-sbxtest", "--env-file", str(empty_env), "-f", "docker-compose.yml",
         "-f", "docker-compose.test.override.yml", "-f", "docker-compose.sbxtest.override.yml",
         "--profile", "test", "config", "--format", "json", "sandbox", "fullstack-tests"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"), "MMR_BUILD_ID": "abc123-test",
             "MMR_SBXTEST_IMAGE": "mmr-sbxtest:abc123-test",
             "DOCKER_CONFIG": os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker"))})


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed")
def test_the_real_rendered_config_of_the_two_services_passes_the_checker(tmp_path):
    rendered = _render(tmp_path)
    if rendered.returncode != 0:
        pytest.skip(f"docker compose config is not available here: {rendered.stderr[-200:]}")
    config = json.loads(rendered.stdout)
    assert config["name"] == "mmr-sbxtest"
    assert set(config["services"]) == {"sandbox", "fullstack-tests"}      # ib-gateway et al. are not rendered
    assert problems(config, operator_home=str(Path.home()), repo_root=str(ROOT)) == []
    images = [s.get("image") for s in config["services"].values()]
    assert images and all(str(img).startswith("mmr-sbxtest:") for img in images)   # never mmr:latest
    assert len(set(images)) == len(images)                                          # one tag per built service
    assert [p for s in config["services"].values() for p in s.get("ports", []) or []] == []


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed")
def test_the_test_runner_waits_for_a_healthy_sandbox_and_shares_its_volumes(tmp_path):
    rendered = _render(tmp_path)
    if rendered.returncode != 0:
        pytest.skip(f"docker compose config is not available here: {rendered.stderr[-200:]}")
    runner = json.loads(rendered.stdout)["services"]["fullstack-tests"]
    assert set(runner["depends_on"]) == {"sandbox"}                          # no trader, no ib-gateway
    assert runner["depends_on"]["sandbox"]["condition"] == "service_healthy"
    mounts = {v["target"]: v["source"] for v in runner["volumes"]}
    assert mounts["/store"] == "mmr_authored_strategies"
    assert mounts["/run/sandbox-live"] == "mmr_sandbox_live"
    assert mounts["/run/sandbox-research"] == "mmr_sandbox_research"
    assert mounts["/var/run/docker.sock"] == "/var/run/docker.sock"            # the base mount is kept


def test_only_the_sbxtest_override_gives_the_test_runner_the_sandbox_volumes():
    # The trader-facing fullstack run uses compose project `mmr`; its runner must never mount the store or sockets.
    base = {v["source"] for v in volumes(load_compose()["services"]["fullstack-tests"])}
    assert not {"mmr_authored_strategies", "mmr_sandbox_live", "mmr_sandbox_research"} & base


def _run_the_container_module(tmp_path, **env):
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "docker.py").write_text("def from_env():\n    raise RuntimeError('stub')\n")
    clean = {k: v for k, v in os.environ.items() if k not in ("MMR_FAKE_BROKER", "MMR_SBXTEST_STRICT")}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "tests/fullstack/test_sandbox_service.py", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
        env={**clean, "PYTHONPATH": str(stub), **env})


def test_outside_the_runner_the_container_module_skips(tmp_path):
    result = _run_the_container_module(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "skipped" in result.stdout and "passed" not in result.stdout


def test_in_strict_mode_a_missing_precondition_fails_the_module_instead_of_skipping(tmp_path):
    # The override sets MMR_SBXTEST_STRICT=1; here the socket volumes are missing (this host is not the container).
    result = _run_the_container_module(tmp_path, MMR_FAKE_BROKER="1", MMR_SBXTEST_STRICT="1")
    assert result.returncode != 0
    assert "skipped" not in result.stdout.splitlines()[-1]
    assert "socket volumes are not mounted" in result.stdout + result.stderr
