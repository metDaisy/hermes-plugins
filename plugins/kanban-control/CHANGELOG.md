# Changelog

All notable Kanban Control changes are recorded here. Versions follow Semantic Versioning.

## [0.5.1] - 2026-10-03

### Fixed

- Normalized Desktop-generated run ids to whole UTC seconds so they always satisfy the Python command parser contract.

## [0.5.0] - 2026-10-03

### Fixed

- Preserved the PM request payload exactly after command metadata, including outer whitespace and trailing newlines.
- Validated the effective configured board before creating the manager card.
- Correlated Desktop polling with a unique run id instead of a truncated request title.
- Scoped Desktop polling timers to plugin disposal and made baseline, timeout, and open failures observable.
- Required the PM workflow to persist and read back aggregate review admission before reviewer promotion.

## [0.4.1] - 2026-10-03

### Fixed

- Updated the Desktop composer middleware registration to the current `ctx.register(...)` SDK contract.

## [0.4.0] - 2026-10-03

### Changed

- Made `run` optional while preserving `/kcp run ...` compatibility.
- Made board-routing parameters optional and preserved the remaining PM request text.
- Removed the dedicated KCP HUD and Dashboard API.
- Added invisible Desktop integration that opens a newly created PM worker session in Hermes' native top tab strip.

## [0.3.0] - 2026-10-03

### Added

- Added the experimental composer HUD and worker-session status projection.

## [0.2.0] - 2026-10-03

### Added

- Added explicit `--board` and `--new-board` routing with pre-dispatch board creation and validation.

## [0.1.0]

### Added

- Initial `/kcp run` command and profile-configured Kanban workflow dispatch.