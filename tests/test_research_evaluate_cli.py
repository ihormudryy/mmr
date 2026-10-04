import argparse
import json
import logging

import pytest

from tests.research.evaluation_fixtures import (
    build_spec_file, write_costs_config, write_trend_bars, write_universe,
)
import trader.mmr_cli as cli
from trader.research.evaluation import EvaluationPaths


@pytest.fixture
def cli_env(tmp_path, tmp_duckdb_path, monkeypatch):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0006)
    costs = write_costs_config(tmp_path / 'execution_costs.yaml')
    paths = EvaluationPaths(tmp_duckdb_path, tmp_duckdb_path, 'Universes', str(costs),
                            tmp_path, tmp_path / 'reports', tmp_path / 'artifacts' / 'evaluations')
    monkeypatch.setenv('MMR_RESEARCH_DUCKDB', str(tmp_path / 'research.duckdb'))
    monkeypatch.setattr(cli, '_evaluation_paths', lambda: paths)
    monkeypatch.setattr(cli, '_signing_key_paths',
                        lambda: (tmp_path / 'keys' / 'signing.pem', tmp_path / 'keys' / 'verify.pem'))
    monkeypatch.setattr(cli, '_artifacts_root', lambda: tmp_path / 'artifacts')
    monkeypatch.setattr(cli, '_json_mode', True)
    return tmp_path


def _json_out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_parser_has_the_new_commands():
    parser = cli.build_parser()
    args = parser.parse_args(['research', 'evaluate', 'spec.yaml', '--dry-run', '--workers', '2'])
    assert (args.spec, args.dry_run, args.workers) == ('spec.yaml', True, 2)
    assert parser.parse_args(['research', 'attest', 'bundle', 'abc']).artifact_id == 'abc'
    with pytest.raises(SystemExit):
        parser.parse_args(['research', 'attest', 'paper', '--decision-id', 'd'])


def test_review_submit_requires_reviewer_kind():
    base = ['research', 'review', 'submit', '--artifact-id', 'a', '--decision-id', 'd',
            '--reviewer', 'r', '--economic-rationale', 'x', '--edge-survives-costs', 'x',
            '--known-failure-regimes', 'x', '--data-limits', 'x', '--parameter-sensitivity', 'x',
            '--operational-dependencies', 'x', '--capacity-and-decay', 'x',
            '--episode-dominance', 'x', '--holdout-opened-once']
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(base)
    assert cli.build_parser().parse_args(base + ['--reviewer-kind', 'llm']).reviewer_kind == 'llm'


def test_review_submit_records_the_reviewer_kind(cli_env, capsys):
    base = ['research', 'review', 'submit', '--artifact-id', 'a', '--decision-id', 'd',
            '--reviewer', 'r', '--economic-rationale', 'x', '--edge-survives-costs', 'x',
            '--known-failure-regimes', 'x', '--data-limits', 'x', '--parameter-sensitivity', 'x',
            '--operational-dependencies', 'x', '--capacity-and-decay', 'x',
            '--episode-dominance', 'x', '--holdout-opened-once', '--reviewer-kind', 'llm']
    cli._handle_research_review(cli.build_parser().parse_args(base))
    digest = _json_out(capsys)['data']['review_digest']

    from trader.research.review import OperatorReviewRepository
    assert OperatorReviewRepository(cli._research_db()).get(digest).reviewer_kind == 'llm'


def test_dry_run_counts_jobs(cli_env, capsys):
    spec = build_spec_file(cli_env)
    cli._handle_research_evaluate(argparse.Namespace(spec=str(spec), dry_run=True, workers=1))
    data = _json_out(capsys)['data']
    assert data['parameter_points'] == 5 and data['walk_forward_jobs'] == 2 * (3 + 4)


@pytest.mark.timeout(240)
def test_evaluate_then_list(cli_env, capsys):
    spec = build_spec_file(cli_env)
    cli._handle_research_evaluate(argparse.Namespace(spec=str(spec), dry_run=False, workers=1))
    result = _json_out(capsys)['data']
    assert result['stage'] == 'pre_holdout'
    cli._handle_research_evaluations(argparse.Namespace(limit=5))
    assert _json_out(capsys)['data'][0]['family_id'] == result['family_id']


@pytest.mark.timeout(240)
def test_json_evaluate_with_workers_prints_one_json_document(cli_env, capfd, monkeypatch):
    spec = build_spec_file(cli_env)
    monkeypatch.setenv('HOME', str(cli_env / 'home'))  # spawned workers set up logging under HOME
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)  # what main() does without --debug
    try:
        cli._handle_research_evaluate(argparse.Namespace(spec=str(spec), dry_run=False, workers=2))
    finally:
        logging.disable(previous)

    assert json.loads(capfd.readouterr().out)['data']['stage'] == 'pre_holdout'


def test_bad_spec_exits_non_zero(cli_env):
    spec = build_spec_file(cli_env, conids=[1001])
    with pytest.raises(SystemExit) as exc:
        cli._handle_research_evaluate(argparse.Namespace(spec=str(spec), dry_run=False, workers=1))
    assert exc.value.code == 1


def test_missing_spec_file_exits_non_zero_and_names_the_path(cli_env, capsys):
    missing = cli_env / 'typo.yaml'
    with pytest.raises(SystemExit) as exc:
        cli._handle_research_evaluate(
            argparse.Namespace(spec=str(missing), dry_run=False, workers=1))
    assert exc.value.code == 1
    out = _json_out(capsys)
    assert out['success'] is False and str(missing) in out['message']


def test_period_too_short_for_the_walk_forward_settings_exits_non_zero(cli_env, capsys):
    spec = build_spec_file(cli_env, period={'start': '2024-02-01', 'end': '2024-02-07'})
    with pytest.raises(SystemExit) as exc:
        cli._handle_research_evaluate(argparse.Namespace(spec=str(spec), dry_run=False, workers=1))
    assert exc.value.code == 1
    out = _json_out(capsys)
    assert out['success'] is False and 'holdout' in out['message']


def test_attest_unknown_artifact_exits_non_zero(cli_env):
    with pytest.raises(SystemExit) as exc:
        cli._handle_research_attest(argparse.Namespace(attest_action='bundle', artifact_id='nope'))
    assert exc.value.code == 1


def test_attest_bundle_reports_the_bundle_and_its_expiry(cli_env, capsys, monkeypatch):
    bundle = cli_env / 'artifacts' / 'sha256_abc'
    bundle.mkdir(parents=True)
    (bundle / 'attestation.json').write_text(json.dumps({'expires_at': '2027-01-02T03:04:05+00:00'}))
    monkeypatch.setattr('trader.research.attest_export.attest_and_export',
                        lambda *args, **kwargs: bundle)

    cli._handle_research_attest(argparse.Namespace(attest_action='bundle', artifact_id='art-1'))

    data = _json_out(capsys)['data']
    assert data['artifact_id'] == 'art-1' and data['bundle'] == str(bundle)
    assert data['expires_at'] == '2027-01-02T03:04:05+00:00'
    assert data['public_key_id']


def test_attest_bundle_still_reports_when_expiry_cannot_be_read(cli_env, capsys, monkeypatch):
    bundle = cli_env / 'artifacts' / 'sha256_abc'
    bundle.mkdir(parents=True)
    monkeypatch.setattr('trader.research.attest_export.attest_and_export',
                        lambda *args, **kwargs: bundle)

    cli._handle_research_attest(argparse.Namespace(attest_action='bundle', artifact_id='art-1'))

    data = _json_out(capsys)['data']
    assert data['bundle'] == str(bundle) and 'expires_at' not in data


def test_attest_with_an_insecure_private_key_exits_non_zero(cli_env, capsys):
    from trader.research.signing import generate_private_key_pem
    keys = cli_env / 'keys'
    keys.mkdir()
    (keys / 'signing.pem').write_bytes(generate_private_key_pem())
    (keys / 'signing.pem').chmod(0o644)
    (keys / 'verify.pem').write_bytes(b'public key placeholder')

    with pytest.raises(SystemExit) as exc:
        cli._handle_research_attest(argparse.Namespace(attest_action='bundle', artifact_id='art-1'))

    assert exc.value.code == 1
    assert 'signing.pem' in capsys.readouterr().out
