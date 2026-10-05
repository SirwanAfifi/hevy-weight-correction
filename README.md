# Hevy weight correction

A Python CLI to back up, preview, apply, and verify a one-time correction of
Hevy workout weights.

| Equipment | Correction |
| --- | --- |
| Dumbbell | `recorded weight × 2` |
| Standard barbell | `recorded weight × 2 + bar weight` |
| Other equipment | Unchanged |

Use these rules only if they match how you logged your history. The tool cannot
detect already-correct entries. Barbell conversion assumes plates from one side;
the default bar is 20 kg and must match your equipment. Recognised specialty
bars, including EZ, Smith, and trap bars, are excluded. Review every correction.

## Setup

Requires **Python 3.10+ on macOS/Linux**, with no extra packages, and a Hevy API
key from [developer settings](https://hevy.com/settings?developer).
See [Hevy's API docs](https://api.hevyapp.com/docs/) for access requirements.

Clone the repo, enter its directory, and copy the configuration:

```sh
cp config.example.json config.json
```

Supply `HEVY_API_KEY` through your secret manager, or save the key alone in a
local `.hevy-api-key` file and run `chmod 600 .hevy-api-key`. Never commit it.

Set `default_barbell_weight_kg` in `config.json`. Optional
`bar_weight_overrides` map exercise template IDs or exact titles to bar weights;
IDs take precedence. All weights use kilograms.

## Plan, apply, verify

```sh
python3 fix_hevy_weights.py plan
```

Planning makes no updates. Review `hevy-correction/summary.md` and `preview.csv`;
the same directory holds the original backup, saved plan, and later run reports.

**Confirm workout visibility first.** Hevy's GET omits `is_private`, while its
write schema documents a public default. Updates are blocked until visibility
is known. Set `default_is_private` to `false` only if all affected workouts are
confirmed public, or `true` if all are private. For mixed visibility, leave it
`null` and map affected workout IDs to their existing booleans in `workout_privacy`.

After changing configuration, rebuild the plan from the same backup:

```sh
python3 fix_hevy_weights.py refresh-plan
```

Review it again, then apply one workout:

```sh
python3 fix_hevy_weights.py apply --limit 1
```

Inspect it in Hevy before continuing:

```sh
python3 fix_hevy_weights.py apply
python3 fix_hevy_weights.py verify
```

Each apply command prompts for `APPLY`. If interrupted, resume the **same plan**;
completed corrections are skipped. **Never generate a fresh plan from corrected
history**, which would calculate another doubling. There is no automatic rollback.

## Safeguards and limits

The tool checks for intervening edits before writing, verifies each update,
and compares the full history afterward. Unexpected differences stop the run.
Server modification timestamps and exact known catalogue-name refreshes for
the same targeted exercise ID are allowed; name refreshes are recorded.

Verification covers API-visible fields. Visibility cannot be read back, and
routine links can be checked but not restored through the documented API.
Avoid concurrent edits: the API has no conditional writes.

Keep the run directory private. Keys, local configuration, and default outputs
are Git-ignored; put custom outputs under `.private/`. Never force-add them.

## Development

Tests use synthetic data without contacting Hevy:

```sh
python3 -m unittest -v
```

Before pushing, stage only intended public files and check the exact Git index:

```sh
python3 scripts/check_public_files.py
git diff --cached --stat
```

The checker enforces a public-file allowlist and checks for common secrets and
private identifiers. Review the diff too; it cannot recognise every private detail.

Inspired by a logging issue pointed out by [Mahdi](https://mahdi.uk/).
Released under the [MIT license](LICENSE).
