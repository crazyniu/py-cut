# Local dedup account preferences (JSON v1)

Profiles contain non-sensitive processing preferences only. They do not log into a
platform, upload videos, or store/use platform credentials. Omitting profile options
retains existing dedup behavior, defaults, and CLI compatibility.

## Usage and precedence

```json
{
  "version": 1,
  "accounts": {
    "main_channel": {"preset": "light", "seed": 101, "border": false, "psnr_min": 22.0},
    "second_channel": {"preset": "standard", "seed": 202, "border": true, "badge": true}
  }
}
```

```bash
./run.sh dedup --file /media/authorized.mp4 \
  --profile-file profiles.local.json --account main_channel

# Explicit CLI values override this account only
./run.sh dedup --file /media/authorized.mp4 \
  --profile-file profiles.local.json --account second_channel \
  --preset light --no-border --seed 888
```

Precedence: explicit CLI > selected-account JSON > original CLI defaults. Boolean
pairs --mirror/--no-mirror, --speed/--no-speed, --border/--no-border, and --badge/
--no-badge provide explicit overrides. Read account values only when those flags
were absent. Selecting one account never changes another account's stored preferences.

## JSON contract

- Top-level keys are exactly version:1 and accounts. accounts is a nonempty object
  containing at most 100 aliases.
- Aliases are 1..64 ASCII letters/digits/dots/underscores/hyphens, starting with a
  letter or digit.
- Each account has at least one preference. Allowed fields: preset (light/standard/
  aggressive), seed (integer 0..2^32-1), mirror/speed/border/badge (JSON booleans),
  phash_min (0..64), psnr_min (0..100), and workers (integer 0..64).
- Reject duplicate/unknown keys, invalid numbers, unknown accounts, incomplete
  --account/--profile-file pairs, and files larger than 64 KiB with nonzero exit.
  Never silently fall back to another account.
- Reject passwords, cookies, tokens, API keys, gateway URLs, real account identity,
  arbitrary FFmpeg commands, and all other unknown fields. Do not echo their values.

Zero/absent seed retains source-path derivation. Fixed path/seed/preset/switches
reproduce parameter choices; environment/FFmpeg can change encoded bytes.

When JSON reporting is enabled, account_profile records alias, schema version,
whitelisted effective preferences, and sources (cli/account/default). Do not record
profile path or credentials. --no-report still disables reporting.

## Integration boundary

commands/dedup_cmd.py resolves profiles using core/dedup_profiles.py and existing
preset, DedupOptions, process_batch, and write_report behavior. Do not add an encoder,
change calibrated presets/gates, or alter overwrite safety. --file/--src-dir/--all
still control input scope. Output directory, --force, and --in-place are not profile
fields. See [DEDUP.md](DEDUP.md) and [../AGENTS.md](../AGENTS.md).
