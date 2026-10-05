# Hevy weight correction

A small Python CLI for a one-time correction of workout weights in Hevy. It
downloads a backup, produces a reviewable plan, applies that saved plan, and
verifies the result against the original history.

The correction rules are:

| Equipment | Correction |
| --- | --- |
| Dumbbell | `recorded weight × 2` |
| Standard barbell | `recorded weight × 2 + configured bar weight` |
| Other equipment | Unchanged |

**Doubling dumbbell weights is a chosen logging convention for this migration,
not a universal Hevy recommendation.** Use it only if it matches the correction
you intend. This CLI applies the rules to every matching historical set; it
cannot tell whether some of those sets were already logged correctly.

Barbell correction assumes the recorded value contains the plates from one side
only. The example configuration uses a 20 kg bar; set it to the actual bar you
used. Equipment metadata determines eligibility. Exercises named as EZ, Smith,
trap, hex, safety, Swiss, landmine, fixed, or T-bar variations are excluded even
when their metadata says `barbell`. Other specialty bars may need additional
exclusions; review the preview before applying.

Mahdi ([mahdi.uk](https://mahdi.uk/)) pointed out the original logging issue that
prompted this project.

## Requirements

- Python 3.10 or later on macOS or Linux. No third-party packages are required.
- A Hevy API key from [developer settings](https://hevy.com/settings?developer).
  See the [official API documentation](https://api.hevyapp.com/docs/) for access
  requirements and the current API contract.
- The existing visibility of each affected workout, checked in Hevy.

## Set up

Clone this repository, enter its directory, and copy the example configuration:

```sh
cp config.example.json config.json
```

Save your API key without putting it in shell history or this repository's
tracked files. This command prompts with hidden input and creates a local file
that is readable only by its owner; it refuses to overwrite an existing file.

```sh
python3 -c 'import getpass, os; key = getpass.getpass("Hevy API key: ").strip(); fd = os.open(".hevy-api-key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.write(fd, key.encode()); os.close(fd)'
```

Alternatively, supply `HEVY_API_KEY` through your usual secret manager. Never
paste a key into an issue, commit, or shared terminal transcript.

The example configuration deliberately leaves visibility unknown:

```json
{
  "default_barbell_weight_kg": 20,
  "bar_weight_overrides": {},
  "default_is_private": null,
  "workout_privacy": {}
}
```

`bar_weight_overrides` maps an exercise template ID or exact exercise title to
its bar weight in kilograms; an ID takes precedence over a title. Overrides
change the bar weight for eligible exercises, but do not enable excluded
specialty bars. All API weights are in kilograms, regardless of app display
units. Null weights and zero dumbbell weights stay unchanged. Zero barbell plate
weight becomes the configured empty bar weight.

## Plan and review

```sh
python3 fix_hevy_weights.py plan
```

This only reads from Hevy. It paginates through the history and saves these
private local files in `hevy-correction/`:

| File | Purpose |
| --- | --- |
| `backup.json` | Original workouts, exercise templates, and account identifier |
| `plan.json` | Exact before/after weights, configuration, and backup checksum |
| `preview.csv` | Set-by-set correction list |
| `summary.md` | Totals, affected exercises, exclusions, and blockers |

Review the summary and every proposed correction. Do not apply a plan if its
rules do not match how you logged the affected exercises.

Hevy's workout GET does not expose `is_private`, while its update schema says
omitting that field defaults to public. The CLI therefore blocks updates until
visibility is known. In your local `config.json`, set `default_is_private` to
`false` only if you have confirmed all affected workouts are public, or `true`
if all are private. For mixed visibility, leave the default as `null` and map
each affected workout ID to its existing boolean in `workout_privacy`. The IDs
are available in the private preview. Do not guess.

After correcting configuration, rebuild the plan from the **same original
backup**:

```sh
python3 fix_hevy_weights.py refresh-plan
```

This does not download or update history. It refuses to run once an apply
journal exists. Review the refreshed preview before proceeding.

## Apply and verify

Start with one workout and inspect it in Hevy:

```sh
python3 fix_hevy_weights.py apply --limit 1
```

The command requires you to type `APPLY`. After checking that workout, complete
the **same saved plan** and verify all workouts from the original backup:

```sh
python3 fix_hevy_weights.py apply
python3 fix_hevy_weights.py verify
```

`journal.json` records progress before and after each update, including the
post-update check. `verification.json` stores the final comparison. Back up the
whole run directory somewhere private; it is intentionally excluded from Git.

If a request fails or a run is interrupted, repeat `apply` with the same run
directory. The CLI re-reads each workout and skips results that already match
the correction. It does not blindly retry PUT requests. **Never generate a new
plan from corrected history:** that would calculate another doubling. There is
no automatic rollback command.

Useful options:

```sh
# Use a different private output directory for every command in a migration.
python3 fix_hevy_weights.py plan --run-dir .private/migration

# Build a plan from an existing download in this tool's backup format.
python3 fix_hevy_weights.py plan --from-backup .private/source.json

# See configuration, key-file, and other options.
python3 fix_hevy_weights.py --help
```

`--yes` skips the `APPLY` prompt for a previously reviewed plan. It does not skip
validation or preservation checks.

## What is checked

Before any write, the CLI validates the entire plan against the original
backup and checks all affected workouts for intervening edits. It checks each
workout again immediately before its update and fetches it afterward to compare
with the intended result. Reps, set types, ordering, notes, timestamps, routine
links, supersets, RPE, custom metrics, and unrelated weights must remain the
same. Unknown fields cause a stop rather than being silently dropped. An
exclusive process lock prevents concurrent CLI runs using the same plan.

The server-managed `updated_at` timestamp is excluded from comparison. In an
updated workout, a targeted dumbbell or standard-barbell exercise's read-only
display title may also be refreshed by Hevy to the exact canonical title in the
backed-up exercise template. This exception requires the same template ID and
is recorded in the journal and verification report. It does not allow arbitrary
title changes or title changes in untouched workouts.

The API has no conditional writes, so avoid editing affected workouts during
the migration. It also provides no writable `routine_id`; the CLI checks that
the server retained it after an update. Data absent from the public API cannot
be backed up or compared by this tool, and visibility cannot be independently
verified through GET. Check those settings in the app.

## Keep account data out of Git

Credentials, local configuration, JSON/CSV exports, backups, plans, reports, and
default run directories are ignored. Only `config.example.json` is allowed as a
public JSON file. Keep custom output directories inside `.private/`.

Before publishing changes, stage only the intended public files and run:

```sh
python3 scripts/check_public_files.py
git diff --cached --stat
```

The checker examines the Git index against an explicit file allowlist, rejects
common credential formats, UUIDs, and local home paths, and checks for your local
Hevy key when present. CI runs it too. It is a guardrail, not a substitute for
reviewing the staged diff: private details can appear in ordinary prose or code.
Do not force-add ignored output or copy real workout data into tests.

## Development

```sh
python3 -m unittest -v
```

Tests use synthetic fixtures and simulated API responses. They never contact
or modify a Hevy account. CI runs on Python 3.10 and 3.12.

Released under the [MIT license](LICENSE).
