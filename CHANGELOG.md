# Changelog

All notable changes to `deltactl` are documented here. The project is pre-1.0;
minor releases may contain breaking API changes, which are called out explicitly.

## [0.3.0] - 2026-11-05

### Added

- Read-only `S` (step relay), `T` (timer), and `C` (counter) support across all
  five canonical profiles.
- Explicit view grammar: `S0`, `T0.c` / `T0.v`, `C0.c` / `C0.v`; bare `T`/`C` is
  rejected so the contact and value views are never guessed.
- `T0.v` and `C0.v–C199.v` as `u16`; `C200.v–C254.v` as `i32` using one wire
  slot per counter and two CDAB registers, read one PDU per counter.
- Raw types `s`, `tc`, `tv`, `cc`, `cv`, and `raw_type_for(address)`.
- Hardware-faithful mock support and opt-in HIL cases for S/T/C, including the
  `0x0EC9` slot-addressing proof.
- `dvp_map` anchors, counts, and read ranges for S/T/C.

### Changed

- **Breaking (pre-1.0):** `SeriesProfile` is now
  `SeriesProfile(name, aliases, devices)`. The flat `d_ranges`,
  `d_split_boundary`, `m_ranges`, `m_split_boundary`, `x_max_octal`,
  `y_max_octal`, `guarded_d`, and `guarded_m` fields are removed. Profiles are
  no longer hashable. Use `profile.devices[Device.D].views[View.WORD].segments`
  (and the equivalent views) instead of the removed attributes.
- `wire_for` and `block_for` take a keyword-only `view`; `function_code_for`
  takes an optional `view` and requires one for `T`/`C`.
- D/M/X/Y addressing is pinned by a differential harness to the 0.2.0 behaviour.

### Fixed

- `read_planned` validates tagless groups and rejects slot-straddling groups
  client-side, before any PDU is sent.
- X/Y range errors display octal bounds again.
- Profile intersection preserves duplicate-key segments and keeps an empty
  intersection empty.
- The mock tolerates both pymodbus 3.16.1 and 3.16.2 request-handler signatures.

### Notes

- T/C/S writes are refused by driver policy; hardware writability is per the
  Delta manual.
- `C200.v–C254.v` slot addressing is mock-tested and HIL-unverified as of this
  release.

## [0.2.0] - 2026-10-31

### Added

- Multi-series hardware profiles and `D1001` model detection.
- Benches-faithful mock server, poller, watch mode, and `doctor` diagnostics.
- Typed 16/32-bit access with Delta CDAB word order and write guards.

### Changed

- Renamed the project to `deltactl`.
