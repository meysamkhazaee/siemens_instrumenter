# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-11-24

### Added
- Centralized `VERSION` file as the single source of truth, dynamically read by `instrumenter.py`, `batch_instrument.py`, `run_test_scenarios.py`, and bundled in PyInstaller builds via `build.py` and `instrumenter.spec`.
- Full EFL (Execution-based Fault Localization) dataset pipeline integration:
  - `cmp.v0.v<id>.<repo>.csv`: 3-field standard test ID, strict tag (`T`, `CC`, `F`), and P-point trace.
  - `source_map.csv`: complete mapping of P-points to source files, line numbers, and functions.
  - `source_changes.json`: fault locations, mutation kinds, and reference lines.
  - `metadata.json` and `cache_manifest.json`: dataset provenance and SHA-256 integrity hashes.
  - Automatic mirroring into standard EFL hierarchy `output/results/<repo>/<version>/full/`.
- Automated dataset synchronization script: `sync_efl_datasets.py`.
- Coincidental Correctness (CC) classification and dual dataset generation (`_dataset.txt` binary vs. `_tagged.txt` 3-state).

### Changed
- Refactored `run_test_scenarios.py` to auto-export EFL artifacts during multi-threaded execution.
- Standardized command line `--version` across all runner tools.

## [1.1.0] - 2026-10-16

### Added
- Multi-threaded batch instrumentation (`batch_instrument.py`) with concurrent worker pools.
- Support for legacy K&R C declarations, archaic function prototypes, and older C dialects in Siemens benchmarks.
- Clang/GCC compiler discovery and cross-platform binary compilation.
- Automated removal of physical AST duplicate markers.

## [1.0.0] - 2026-10-16

### Added
- Initial C/C++ AST-based basic block and execution path instrumenter.
- Clang AST JSON/text dump parser and P-point marker injector.
- Standalone executable packaging via PyInstaller (`build.py`).
