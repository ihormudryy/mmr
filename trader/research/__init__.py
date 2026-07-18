"""Offline research-evidence foundation (Trading Income P2).

A separate research DuckDB stores immutable dataset manifests, universe
membership, experiment families, trials, validation folds, metrics, reviews, and
attestations. This package is NEVER imported by trader_service / strategy_service
and never opens the journal/history/operational databases.
"""
