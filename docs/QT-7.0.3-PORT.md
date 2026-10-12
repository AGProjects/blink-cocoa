# Blink 7.0.3 — porting Qt changes to Cocoa

2026-10-12 · Adi

## Summary

Cocoa has almost none of the 15 Blink Qt patches between 7.0.2 and 7.0.3. In several places it still has the bugs those patches fixed.

- **Scope:** Qt patches recorded 2026-10-12, from "Generate PGP key silently only after addressbook fetch" to "Added version 7.0.3".
- **Method:** current code compared on both sides (`~/work/blink-qt/blink/`, `~/work/blink-cocoa/`). Darcs diffs were not read. Line numbers are from the Cocoa tree as synced to the Linux machine on 2026-10-12 and may drift on the Mac.
- **Not reviewed:** Qt `chatwindow.py`, where the in-view decrypt path (`ConversationModel.decrypt_rows`) lives.
- **Order below:** SylkServer discovery first, because PGP gating, key lookup and test numbers all depend on it. Then the journal and addressbook fixes, then PGP, then background decryption.

| Qt patch | Cocoa today | Step |
| --- | --- | --- |
| Don't attempt PGP key lookups and generation without server support | Missing; no SylkServer discovery | 1 |
| Fix resuming 1st journal import if Blink quits | Bug: resumed run treated as catch-up | 2 |
| Fetch call history from server after AB has stabilized | Bug: fetched on account activation | 3 |
| Create test contacts only if they do not exist | Bug: hardcoded ids on first run, duplicates | 3 |
| Don't modify addressbook from old journal entries | Bug: refiles, creates and revives contacts | 4 |
| Skip individual journal entry logging at 1st sync | Partial | 4 |
| Generate PGP key silently only after AB fetch and server says no key | Partial; no server lookup | 5 |
| Log PGP key status at start | Partial | 5 |
| Skip pgp warnings | Partial | 5 |
| Show PGP key ids in message info | Missing | 5 |
| Decrypt old messages that come into view | Missing (page decrypt on open) | 6 |
| Decrypt stored PGP messages slowly in a background thread | Missing | 6 |
| Improve decryption speed | Cocoa already has the privkey cache | 6 |
| Added speed control for decrypting CPU resources | Missing | 6 |
| Dispatch decryption to multiple cores | Missing; see decisions | 6 |

## Step 1: SylkServer discovery and PGP gating

Port `sylk_discovery.py` first. PGP works only for Bonjour and for accounts whose domain runs SylkServer, and Cocoa has no way to tell.

**Qt**

- `sylk_discovery.has_sylkserver` (53) is true when a `_sylkserver.<domain>` TXT record pointing to a JSON config was found, now or on an earlier run (cached on disk).
- Changes post `SylkServerConfigurationDidChange`. The config also carries `testNumbers`, used in step 3.
- Checked in four places:
    - `messages._pgp_messaging_possible` (1696), before any key generation.
    - `check_key_status` (3010), so nothing is logged for non-SylkServer accounts.
    - `lookup_server_key` (3223), which records `unsupported`.
    - `mainwindow._update_pgp_key_actions` (1355), which disables Export and Generate private key. It refreshes on `SylkServerConfigurationDidChange` (741).
- Bonjour is exempt. Lookups of contacts' public keys are not gated (`OutgoingMessage` 575, `_fetch_public_keys_once` ~2451).

**Cocoa today: missing**

- No discovery module.
- `ContactWindowController.updateBlinkMenu` (~7284) enables Export (tag 67) only on "not Bonjour and a `.privkey` exists".
- Generate (`generateKeyButtonClicked_`, 1532; tag set only in the nib) has no validation and is always enabled.

**Port**

1. Add `SylkDiscovery.py` from `sylk_discovery.py`. It is self-contained apart from `call_in_gui_thread` → `run_in_gui_thread`. Use the bundled DNS resolver; put the cache under `ApplicationData`. Post the same notification.
2. Gate generation and the prompt in `SMSWindowManager.showGeneratePrivateKeyPanel` (6748) and `SMSViewController.load_private_key` (758), and the key status log (step 5).
3. In `menuNeedsUpdate_`, disable Export, Generate and the escrow items (tags 69/70, `updateKeyEscrowMenuItem`) for accounts without SylkServer.
4. Expose `testNumbers` for step 3.

## Step 2: Journal first-sync resume

A first sync interrupted by quitting must continue as a first sync after restart. In Cocoa it turns into an ordinary catch-up and the activation announcement is never sent. Steps 3 and 4 key on the marker added here.

**Qt** (`messages.py`, `journal.py`, `history.py`)

- Persisted marker `journal/<acct>/first-sync.marker` (journal.py:44), written when a first sync starts (messages.py:1082–1090).
- `first_sync = resumed or not cursor`. Every page of a first sync counts as first-sync: `page_first_sync = stats.first_sync or not page.cursor` (1273).
- The marker is removed only by `settle_first_sync_read` (history.py:1343) on `BlinkJournalDidApply(first_sync=True)`.
- `self.quitting` is set on `SIPApplicationWillEnd` (1607) and checked per download chunk (1008), per page (1261) and per entry (1389); each raises `JournalInterrupted`.
- Quit during download (1197): cached pages kept, nothing applied. Next start downloads the rest first, then applies all pages.
- Quit during apply (1322): the page is kept, not counted as a failed attempt; receipts dropped.
- No pages left but marker present: post the settle at once (1222–1231).
- Pages written as `.part` then `os.replace`.

**Cocoa today: partial**

- Download-then-apply order and per-page delete exist (SMSWindowManager.py:2264, 2326).
- `first_sync = not cursor`, taken at run start (2807, 2823). After a quit the cursor is set, so the resumed run replays imdn receipts, shows banners, skips `promoteMessagesGroup` and never calls `announceActivation` (2846).
- Pages 2..N of one first sync carry a cursor, so `syncIncomingMessage`/`syncOutgoingMessage` (5980, 6412) treat them as catch-up.
- Nothing observes quit in `SMSWindowManager`.
- A page is unlinked even when applying it raised (2378–2384).
- Pages are written directly (2301); a partial file from a quit fails to parse and is deleted as applied.

**Port**

1. Add `FIRST_SYNC_MARKER` in `journalDirectory(account)`. Write it when `not history_last_id` and it is absent.
2. `first_sync = exists(marker) or not cursor`; per page `first_sync or not payload['cursor']`.
3. Add `self.quitting` from `_NH_SIPApplicationWillEnd`. Check it per page in `_downloadJournal` and per page and entry in `_applyJournalFiles`/`_applyJournalEntries`. On quit, return without unlinking and without `_finishJournalApply`.
4. Unlink a page only on success.
5. Write pages via `.part` + `os.replace`.
6. Remove the marker after `_finishJournalApply` only when every page applied, then run the first-sync settle (announce, promote).

## Step 3: Addressbook settled — call history and test contacts

Cocoa needs an "addressbook settled" signal. Without it, server call history and test contacts create contacts before the XCAP addressbook arrives, which produces duplicates.

### Qt mechanism

- `ContactRepair` (contacts.py:892): `loaded` fills on each account's first `XCAPManagerDidReloadData` (950). `run()` (961) runs `settle_delay+5` s later, promotes `settled |= loaded`, does repair, kind groups, merge and test numbers, then posts `BlinkAddressbookDidSettle`.
- `addressbook_ready` (917): with XCAP the account must be in `settled`; without XCAP, `done` is enough.

### Call history

- **Qt:** `ServerCallHistory.start` (server_call_history.py:169) parks a not-ready account in `self.waiting` (no timeout). `_NH_BlinkAddressbookDidSettle` (276) waits 5 s, then `_addressbook_settled` (280) starts it.
- **Cocoa (bug):** `SessionHistoryReplicator._NH_SIPAccountDidActivate` → `get_last_calls` (HistoryManager.py:3661/3678) fetches at once and starts the 300 s timer. `syncServerHistoryWithLocalHistory` posts `AudioCallLoggedToHistory` (3940, 4061) → `ContactListModel._NH_AudioCallLoggedToHistory` (5927) → `ensure_call_contact(..., xcap_loaded=True)` (HistoryManager.py:5368). Contacts and Calls/Tel groups get created before the addressbook arrives.

### Test contacts

- **Qt:** `ContactRepair.file_test_numbers` (contacts.py:1000). Source is SylkServer `testNumbers`. Skipped unless `addressbook_ready`. Excludes URIs already in per-profile `test_numbers.json` (982) and in-flight `_test_numbers_pending`. `_write_test_numbers_now` (1051) matches by canonical URI: an existing contact is only filed into Test (named if nameless), Deleted/Blocked contacts are left alone, otherwise a new contact is created. Re-runs on `SylkServerConfigurationDidChange`.
- **Cocoa (bug):** `createInitialGroupAndContacts` (ContactListModel.py:5110) runs from `_NH_SIPApplicationDidStart` on `first_run` (5848), before any XCAP reload. It uses hardcoded ids `test_call`/`test_conference` with sip2sip URIs and guards only on `DuplicateIDError`, never on URI.

### Port

1. Post `BlinkAddressbookDidSettle` (with the account) at the end of `ContactListModel._callsGroupAfterReload` (5885), after `mergeMessagesGroupDuplicates`. For accounts without XCAP, post it from the `calls_group_startup_check` path. Keep a `settled` set.
2. In `get_last_calls`: if `account.xcap.enabled` and not settled, park it; on the notification wait ~5 s and start it.
3. Drop the `first_run` call to `createInitialGroupAndContacts`.
4. Add `file_test_numbers()` after the settle, with Qt's rules: canonical-URI lookup, file an existing contact rather than create, persisted `test_numbers.json`, skip Deleted/Blocked. Source `testNumbers` from step 1; keep the hardcoded list only as fallback. Find the Test group by id `test`, kind or name.

## Step 4: Old journal entries must not change the addressbook

The addressbook fetched at start is the latest. Replaying old journal entries must leave it alone; missing contacts are added only at the end, and only if still missing. Cocoa breaks this in four places.

**Qt**

- `MessagesGroupFiler` (contacts.py:1216). Journal stores post `BlinkMessageHistoryMessageDidStore(from_history=True)` (history.py:2501); these go to `history_pending`, not `pending` (1279).
- `importing` follows `BlinkJournalProgress` (1285); `_flush` (1310) holds history items until the import ends.
- `_file(..., missing_only=True)` (1358) creates a Messages contact only for keys no addressbook contact has. Existing contacts are not refiled and not revived from Deleted.
- Conversations already in history at start (`AllContactsDidSucceed`) take the same missing-only path.

**Cocoa today: violates it**

- `_finishJournalApply` → `ensureMessagesGroupContains(uri)` for every `sync_contacts` (SMSWindowManager.py:2730): refiles existing contacts into Messages; `getContact` creates new ones (3008–3015). Same at launch in `_applyRestoredUnreadCounts` (4031).
- `text/pgp-public-key` entry → `saveContact` → `getContact(create=True)` (2611, 2933).
- `application/sylk-conversation-remove` → `applyConversationRemoval` → `fileConversationAsDeleted` (4362).
- `_persist_journal_message` → `noteMessageTime` → `_reviveDeletedConversation` (5758 → 4048 → 4376) pulls contacts out of Deleted because an old message replayed.

**Port**

1. While `_journal_bulk` (or a first/resumed sync) is active, collect keys into `history_pending` instead of calling `ensureMessagesGroupContains`.
2. At the end, create contacts only for keys `_findContactByCanonicalURI` does not know, in one transaction, and put them in Messages. Do not refile existing contacts.
3. Gate `_reviveDeletedConversation` and `fileConversationAsDeleted` on `not self._journal_bulk`. History rows are still hidden; the addressbook is untouched.
4. Public key from the journal: store the key file; update only an existing contact (`getContact(create=False)`).

### First-sync logging

- **Qt:** `first_sync_pending(account_id)` (history.py:400) is a marker-file check. `history_info` (411), location stores (970), conversation-remove (messages.py:1462) and key lookups (2036) log at debug while it is true.
- **Cocoa:** `syncIncomingMessage` already logs at debug. Still info per entry: "Public key from %s received" (2577), "[cdr] … call … from another device" (1360), "Conversation with %s was read on another device" (6055), "Conversation with %s removed and filed under Deleted" (4363), "Adding contact for %s" (3013), and `ensure_call_contact`'s `_log` lines.
- **Port:** add `first_sync_pending(account)` on the step 2 marker; route those lines to `log_debug` while it is true; rely on `_logJournalSummary` for counts.

## Step 5: PGP key generation, status log, warnings, key ids

Cocoa never generates a key silently, so it avoids the Qt overwrite bug. But it never asks the server whether a public key exists, so after a slow addressbook fetch it can claim "no private key yet" when the server has one.

### Generation gate

- **Qt:** `messages._offer_key_generation` (1687) waits on `KeyEscrowManager.when_answered` (3254); `_key_generation_decided` (1704) calls `may_generate_silently` (3193). True only when the account has XCAP, its addressbook was reloaded (`self.checked`, set in `_NH_XCAPManagerDidReloadData` 3099), no escrow record exists, and `server_key == 'absent'`.
- `lookup_server_key` (3216) sends `sylk-api-pgp-key-lookup` to the account's own address, with an `on_result` callback and 40 s timeout. `_lookup_answered` (3238): 404 "No Public Key" → `absent`, 200 → `present`, else `failed`. The addressbook wait has a 15 s timeout.
- Any other case shows the dialog; a decline is remembered per run. `_generate_keys` runs in a thread, then `KeyEscrowManager.repair()`. Own-key replies go to `server_public_key_received` (808).
- **Cocoa:** `SMSWindowManager.showGeneratePrivateKeyPanel` (6748) always shows an NSAlert. Gate `escrowQuestionAnswered` (6700) checks the addressbook only and passes after 15 s even if it never loaded. No server lookup. `load_private_key` (SMSViewController:758) prompts each time a viewer opens; nothing re-offers when the addressbook arrives. `generate_pgp_keypair` (161) runs inside the modal and does not repair escrow.
- **Port:**
    1. Add `server_key` state, `lookup_server_key`, `_lookup_answered` and a `when_answered`/`_release` queue to `SMSWindowManager` next to `key_escrow_checked`; call `_release` from `_NH_XCAPManagerDidReloadData` (1735).
    2. `sendMessage` (2084) is fire-and-forget and blocks on `.wait()`. Add a variant that observes `SIPMessageDidSucceed`/`DidFail` and returns `code`/`reason` on the GUI thread.
    3. `load_private_key` calls an `offer` that queues on `when_answered`, generates silently when allowed, otherwise prompts.
    4. Run keygen (4096-bit RSA) off the main thread, finish with `@run_in_gui_thread`, then `repairMissingEscrow`.
    5. In the public-key handler (7126), store a reply for our own account instead of skipping it.

### Key status at start

- **Qt:** `KeyEscrowManager.start` (2986) schedules `_log_key_status_of_all` 2 s after start. `check_key_status` (3004) runs once per enabled SylkServer account, at start, on addressbook reload or on registration (1825). `log_key_status` (3031) logs three lines — device key, addressbook escrow, server public key — each with fingerprint (`pgp_keyids.key_fingerprint`) and same/DIFFERENT versus the local key.
- **Cocoa:** only `log_self_contact` on each XCAP reload (1820).
- **Port:** same three lines via `BlinkLogger().log_info`, from `BlinkAppDelegate` after start, on registration, and in `_NH_XCAPManagerDidReloadData`. Needs the server state above and the step 1 gate.

### pgpy warnings

- **Qt:** `__init__.py:16`: `warnings.filterwarnings('ignore', message=r'TripleDES has been moved', module=r'pgpy(\.|$)')` before pgpy is imported.
- **Cocoa:** only `KeyEscrow._pgpy` (89) suppresses it; `SMSWindowManager.py:31` and `SMSViewController.py:32` import pgpy at module level.
- **Port:** same `filterwarnings` at the top of the entry point, before `BlinkAppDelegate` imports `SMSWindowManager`.

### Key ids in message info

- **Qt:** `pgp_keyids.py` — `encrypted_to`, `key_owners` (key id → address, own keys marked, mtime cache), `describe`. `history.py` adds `encryption_keys` (migration 2130), filled in `store_decrypted_message` (1528) and `store_undecryptable_message` (1555) before the ciphertext is overwritten.
- **Cocoa:** `MessageHost.pgp_message_key_ids` (1250) already does `encrypted_to`. `HistoryManager.update_decrypted_message` (1322) overwrites `body`, losing the ids. No column; not shown in `_row_details` (SMSViewController:199) or `NativeChatViewController._messageInfoSections` (3322).
- **Port:** add `ChatMessage.encryption_keys` with an `ALTER TABLE` migration (~243); set it from `pgp_message_key_ids(body)` in `update_decrypted_message` and the failure path; add it to `_row_details`; port `key_owners`/`describe` into `MessageHost`; add an "Encrypted to" row under Encryption, parsing `row['body']` when still armored. Check where Cocoa keeps its own `.pubkey` files (private keys are flat `<account>.privkey`).

## Step 6: Background PGP decryption

Qt now decrypts stored ciphertext in a throttled background pass. Cocoa decrypts one history page when a conversation opens, on the SIP event loop, and never records failures, so they are retried on every page load and preview build.

### Qt design (`pgp_decryptor.py`, `BackgroundDecryptor`)

- **Start:** `__init__.py:557`. First run 30 s after launch; reruns after each `BlinkJournalDidApply` and any `sms.private_key` change. A request during a run sets `_again`.
- **Selection** (`_find`, db thread): ids only, for enabled PGP accounts with a key file, `decrypted in ('0','3') and content like '-----BEGIN PGP MESSAGE-----%'`, newest first.
- **Pipeline:** `_load` (db) → `_decrypt` (thread `pgp-decrypt`; skips rows decrypted meanwhile) → `_store` (db) → `_stored` (GUI) → `call_later(batch_pause, _next)`. Batch = 20 × workers.
- **Workers:** `subprocess.Popen([sys.executable,'-I','-c',WORKER_CODE])`, `os.nice(19)`, pgpy only, JSON lines over stdin/stdout; keys pushed once per key-file mtime; round-robin; 60 s hang timeout; any exception falls back to in-process `_decrypt_here`.
- **Speeds** (active duty / idle duty / s between batches; workers):

| Speed | Active | Idle | Batch pause | Workers |
| --- | --- | --- | --- | --- |
| Fast | 0.8 | 0.9 | 0.2 s | cores−1 at 1.0 |
| Normal | 0.15 | 0.5 | 1.0 s | min(2, cores−1) at 0.5 |
| Gentle | 0.05 | 0.2 | 3.0 s | 1 at 0.15 |

- Sleep after each message = `cpu_time*(1/duty−1)`, minimum 0.02 s. Active/idle from `applicationStateChanged`.
- **Yield to the view:** if `last_view_decrypt` is under 5 s old, the next batch waits (presumably set by `chatwindow.decrypt_rows`; verify).
- **Speed setting:** plain file `<ApplicationData>/pgp_decryption_speed` (`"<speed> <resume_speed>"`). UI is a `QToolButton` menu (Fast/Normal/Gentle/Pause→Resume) by the journal progress bar (mainwindow.py:347–380), with done/total and ETA (1631, 1641). Journal download has priority for the bar.
- **Storage:** success → `store_decrypted_message` (history.py:1528): plaintext replaces ciphertext, `decrypted='1'`, `encryption_keys` kept, category/has_link filled. Failure → `store_undecryptable_message` (1555): `decrypted='2'` + `decryption_error`. Key change → `reset_decryption` (2645) turns 2 into 3 for retry. Worker death leaves the row for the next run.
- **UI:** `_stored` posts `BlinkMessagesDidDecryptInBackground(ids)` plus progress.

### Cocoa today

- Journal stores ciphertext with `encryption='pgp_encrypted'` (SMSWindowManager.py:5352, 5976, 6408), as the directive requires.
- `SMSViewController.replay_history` (4926, `@run_in_green_thread`) calls `_decrypt_history_messages` (5259) for the whole page before rendering, on the reactor. Success → `HistoryManager.update_decrypted_message` (1322) replaces the body, `encryption='verified'`.
- Previews: `_buildMessagePreviews` (4091) decrypts in thread `message_previews` via `_decrypt_pgp_for_account` (3625); no failure memo; key cache `private_keys` never invalidated.
- `MessageHost.install_pgpy_privkey_cache` (1282) already exists.
- `NativeChatViewController.updateMessage` (4173) can refresh a bubble in place.

### Port

1. Schema: `decrypted TEXT DEFAULT '0'` and `decryption_error` (or new `encryption` values); add `store_undecryptable_message`, `reset_decryption` on private-key change, and a batch read by ids.
2. `PGPDecryptor.py`: port `BackgroundDecryptor`; `call_later` → `reactor.callLater`; app state → `NSApplicationDidBecomeActive`/`DidResignActive`; keys via `MessageHost.load_private_keys`/`private_key_for_message`. Start from `BlinkAppDelegate`; rerun after journal apply.
3. Threading: one `run_in_thread('pgp-decrypt')` with the privkey cache and duty-cycle sleep; optionally `pthread_set_qos_class_self_np(QOS_CLASS_BACKGROUND)` via ctypes for E-cores.
4. Move `_decrypt_history_messages` off the reactor to `pgp-decrypt`; set `last_view_decrypt` so the background pass yields.
5. In-view: render armored rows as "Decrypting…", decrypt visible msgids first, then `updateMessage`.
6. `SMSWindowManager` observes `BlinkMessagesDidDecryptInBackground`, forwards to open `SMSViewController`s; previews reload via `loadMessagePreviews(remote_uris)`. Category changes may need a full redraw.
7. Speed control: progress strip + `NSPopUpButton` in `ContactWindowController`, or a simple preference in `PreferenceOptions` (decision below).

## Open decisions and Qt-side follow-ups

Four choices are needed before step 6. Two Qt fixes came out of the review.

### Decisions for Cocoa

- [ ] **Worker processes in the Mac bundle.** In py2app, `sys.executable` may be the app stub (relaunches Blink), and `-I` drops the bundle's library path so pgpy won't import. `multiprocessing` needs `freeze_support`; hardened runtime applies to children. Recommendation: one background-priority thread on macOS. The privkey cache already removes most of the cost.
- [ ] **Plaintext at rest.** Background decryption ends "ciphertext until first open" (SMSWindowManager.py:5328). Cocoa already writes plaintext back once a row is read, so this matters only if that was meant as a privacy feature.
- [ ] **Failure marker.** New `decrypted`/`decryption_error` columns (as Qt), or new values in `encryption`.
- [ ] **Speed control placement.** A progress strip with popup like Qt (Cocoa has no journal progress UI), or a simple preference without ETA.

### Qt follow-ups

- [ ] `WORKER_CODE` does not install the RSA private-key cache, so each worker rebuilds the key per message: about 260 ms versus about 6 ms. Likely a bigger gain than more cores.
- [ ] A worker timeout should fall back to in-process decryption. Today a hung worker costs 60 s per batch.
- [ ] Review the in-view path in `chatwindow.py` (`decrypt_rows`, `last_view_decrypt`), not covered here.
