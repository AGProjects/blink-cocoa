# Profiles

A profile is a set of accounts with their own address book and settings.
Switching profiles lets one Blink installation serve, for example, a work and a
personal identity, or a test setup, without mixing their accounts and contacts.

Available in the Blink target only (`delegate.profiles_enabled`, set in
`branding.py`). Blink Pro and the other brandings carry the code but never
show the menu.

Implementation: `Profiles.py`. Hooks: `BlinkAppDelegate.py` (switch at start,
menu), `HistoryManager.py` (history filter).

## What a profile contains

| In the profile                         | Shared by all profiles                              |
|----------------------------------------|-----------------------------------------------------|
| `config`: accounts, their address book, general settings | `history/history.sqlite` (calls, messages, file transfers), recordings |
|                                        | PGP keys, logs, downloads, photos, map tiles, TLS files |
|                                        | Keychain passwords (keyed by account id), NSUserDefaults |

The profile in use keeps its `config` where Blink and the SDK read it, in
`~/Library/Application Support/Blink/`. The others are kept in
`profiles/<name>/config`.

| File                      | Meaning                                               |
|---------------------------|-------------------------------------------------------|
| `profiles/active`         | Name of the profile in use (`Default` when missing)    |
| `profiles/<name>/config`  | A profile not in use                                  |
| `profiles/switch`         | Profile to use at the next start                      |
| `profiles/delete`         | Profile to put aside at the next start                |
| `profiles/.deleted/<name>-<time>/` | A deleted profile, kept, never erased        |

Names: up to 64 characters, no `/`, `\`, `:` or control characters, not
starting with a dot, and not `active`, `switch` or `delete`.

## Blink > Profiles

The menu sits after Preferences and is filled each time it opens:

- **The profiles**, the one in use ticked. Choosing another asks for
  confirmation and relaunches Blink in it.
- **Save Profile As...** gives the profile in use another name. Nothing is
  copied and Blink does not restart.
- **New Profile...** creates a profile with the general settings of the one in
  use, and no accounts and no address book. Blink then offers to switch to it.
  At its first start it opens the add-account window, as on a first run.
- **Delete Profile...** deletes the profile in use, the only one that can be
  deleted. Blink asks which profile to continue with, lists the accounts that
  go with the deleted one, and relaunches. The deleted profile is moved to
  `profiles/.deleted/` and can be taken back by hand. Disabled when there is
  only one profile.

## Switching

A switch needs a restart, because the SDK reads the configuration once.

1. The menu writes `profiles/switch` (and `profiles/delete` for Delete Profile).
2. Blink relaunches itself: a detached `/bin/sh` waits for the Blink process to
   end, then runs `open` on the bundle. If that cannot be started, Blink tells
   the user to start it again and quits.
3. At the next start, in `applicationDidFinishLaunching_` before the
   configuration is read, `Profiles.apply_pending_switch()` makes the swap:
   - the `config` in use moves to `profiles/<current>/config`;
   - the chosen profile's `config` moves into the data directory;
   - `profiles/active` is updated;
   - a deleted profile is moved to `profiles/.deleted/`.

The activity log shows `[profile]` lines: the profile in use at every start,
switches, creations, renames and deletions.

## History

There is one history database for all profiles, and every row carries its
account (`local_uri`). When there are other profiles, the reads leave out the
accounts that belong to another profile and not to the one in use
(`Profiles.hidden_accounts_sql`, through `HistoryManager._uri_in_sql` and the
queries that take no account). This covers:

- the calls history, recent calls and missed calls;
- conversations, transcripts, categories and counts;
- the last message and unread badges in the contact list;
- deleted conversations.

The behaviour has these properties:

- **The same account in two profiles** shows its history in both.
- **With a single profile nothing is filtered:** the history of accounts
  removed from the configuration stays visible, as before profiles existed.
- **A lookup by message id or call id is not filtered,** so delivery reports,
  journal deduplication and SIP trace links find their row whatever the profile.
- **The list of hidden accounts is read once per start,** which is enough
  because a switch is always a restart.

Not filtered: the MSRP File Transfers window.

## iCloud

Not involved: iCloud account sync is enabled only in Blink Pro, which has no
profiles. A branding that enables both would have to keep iCloud to one
profile, or it would copy accounts between them.

## Blink Qt

Blink Qt has the same feature (`blink/profiles.py`, Blink > Profiles), with
these differences:

- a profile is also its `calls_history` and `test_numbers.json`;
- the restart goes through Qt.
