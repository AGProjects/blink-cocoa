# Porting recent Blink Qt work to Blink for macOS

Blink Qt changes from 16 to 19 October 2026 (tag 7.1.3 and after), compared
with this tree, and what to port. Each item is meant to become one patch,
tested and recorded on its own, as for `QT-7.0.3-PORT.md`. The first sync is
specified for all clients in Blink Qt's `docs/messaging/journal-first-sync.md`;
read it before items 1–7.

## Already done here

On 19 October, after a contact restored on Blink Qt went back to Deleted when
this client started:

- **A received conversation removal hides the conversation only.**
  `SMSWindowManager.applyConversationRemoval` no longer files the contact
  under Deleted, live or from the journal. The device that removed it moved
  the contact to Deleted on the server, and that reaches every device through
  XCAP. A removal missed while offline and delivered at register had been
  deleting the contact again after it was restored.
- **Deleted means Deleted only.** `fileContactAsDeleted` takes the contact out
  of every other group (it used to leave all but Messages), and a restore
  files it back into Tel and Conference by its addresses
  (`refile_contact_by_addresses`), and into Messages when it has a
  conversation.

Still different from Blink Qt here: deleting a contact that is in Blocked does
not remove its presence-block policy (Blink Qt's `ContactTrash._soft_delete`
does, in the same XCAP update).

## Ported on 19 October (to test, one patch per item)

| Item | Where |
|---|---|
| 8 | `util.message_from_the_past`, `notify_new_message(timestamp=)` (`BlinkAppDelegate`), the live and `gotMessage` paths, the journal "while you were away" count |
| 1–7 | `SMSWindowManager`: `_firstSyncRecent`, `_applyJournalEntries(snapshot=)`, `_snapshotIncomingMessage`, `_snapshotReadPoints`, `noteSnapshotRead`, `startSnapshotBacklog` / `_runSnapshotBacklog` (thread `journal-backlog`, utility QoS), `_downloadSnapshotOlder`, `_lookupMissingPublicKeys`, `receiptAllowed` (checked in `SMSViewController._receive_message` and the history replay), `journalImportSpeed` / `setJournalImportSpeed`; the bulk mode is per thread (`_inJournalBulk`) |
| 10 | `JournalProgressStrip.py`: `ProgressLine`, one line per job |
| 11 | `AddressbookChecks.py` (`checklist_dialog`), scheduled from `_NH_XCAPManagerDidReloadData` |
| 12 | `lastMessagePreviewForRow` (address in Deleted), Edit in the Deleted menu with `EditContactController.lockForDeletedContact`, `editContact` leaves groups and subscription alone, counts in `expungeContacts` (`ChatHistory.count_conversation_messages`) |
| 9 | `_syncLog` is debug while a journal is applied by the thread; the per-entry lines use it |
| 13 | `ContactWindowController.logContactContextMenu` |

A first sync begun by the earlier code (marker, no `snapshot/`) is finished the
old way. Files: as in Blink Qt (`first-sync.marker`, `first-sync.date`,
`snapshot/`, `snapshot/older/`, `snapshot/read-points`, `snapshot/backfill`);
the speed is in `journal_import_speed`.

## How the first sync worked here before

`journalSinceWindow()` asks for five years, the pages are applied oldest first
in bulk (`_journal_bulk`, `_applyCachedJournals`), and the first sync is
settled at the end. Nothing is usable until all of it is in, read state is
guessed, and a new device sends receipts for old messages when they are read.

## To port

### High value: the first sync (items 1–7, one series)

1. **The first sync is a snapshot.** Every message has its final state in the
   journal and removed messages are not in it, so a first sync only inserts:
   receipts, conversation reads, message and conversation removals, bulk
   deletes and public keys are skipped. The order of the entries does not
   matter. Partly here already through `_journal_bulk`.
   Blink Qt: `MessageManager.journal_snapshot_skipped`, `_apply_snapshot_entries`.
2. **Read points.** An incoming message has no read state in the journal (only
   outgoing messages carry delivered/displayed). A conversation's read point
   is the later of its latest `application/sylk-conversation-read` marker and
   the user's latest outgoing message in it; an incoming message up to it is
   stored read, a newer one unread. The points are made from all the pages of
   a phase before any of them is applied, and decided at insert time, so no
   badge counts a message a later page would mark read.
   Blink Qt: `_snapshot_read_points`, `_merge_read_points`, `_journal_store`
   (`read=`), `MessageHistory.apply_read_points`; a read on another device
   during the first sync moves the point (`_NH_BlinkConfirmReadMessagesOnOtherDevice`).
3. **The last 30 days first, the rest in the background.** Download `since`
   T0 − 30 days, apply it newest first, finish the first sync (badges counted
   once, contacts filed, Messages group promoted): the client is usable. Then
   a background thread downloads from five years ago up to T0 − 30 days
   (position saved per page, resumed after a quit or a network error), merges
   the read points of all those pages, and applies them newest first.
   Blink Qt: `_fetch_server_history`, `_apply_snapshot_recent`,
   `_apply_snapshot_backlog`, `_download_snapshot_older`, `_run_snapshot_backlog`;
   state in `journal/<account>/snapshot/` (`complete`, `read-points`,
   `backfill`, `older/`).
4. **Pace and speed menu for the older history.** Batches with a pause that is
   a multiple of the time the batch took, in a lower-priority thread; the
   user picks Fast / Normal / Gentle / Pause by its progress bar, kept between
   runs. Here it fits next to the PGP decryption speed control.
   Blink Qt: `journal_import_speeds`, `set_journal_import_speed`,
   `_apply_server_history_messages(pace=)`.
5. **Receipts.** A device never sends a delivery or display receipt for a
   message older than its first sync start (kept for ever in
   `journal/<account>/first-sync.date`) minus 30 days. Within the 30 days a
   message read or received on the new device is confirmed as on any device.
   Importing sends nothing by itself.
   Blink Qt: `MessageManager._before_first_sync`, checked in `send_imdn_message`
   and in the sessionless delivered receipt of a location request.
6. **Public keys once, at the end.** No keys from the journal in a first sync;
   the server is asked for the keys of the contacts that have none when the
   whole of it, older history included, is in.
7. **Badges.** Not counted during the 30 days, set once when they are in;
   during the older history messages arrive with their final read state, so
   badges move only for an old message nobody ever read.

### Medium value

8. **No desktop notification for a message older than the start of the
   application**, whatever path it comes by (a decryption finishing late, a
   message stored on the server while offline). Small and independent: a
   good first patch. Blink Qt: `MessageNotifier._from_the_past`.
9. **Journal entries are logged per entry at debug level only.** The Activity
   log keeps the summaries (sync start and end, pages, first sync finished
   with its unread count, older history complete) and the errors.
10. **Progress lines shown together.** Journal fetch and import, older history
    (with its speed menu), PGP decryption (with its speed menu), each on its
    own line, each shown only when its job takes a while. To check:
    `JournalProgressStrip.py` today.
    Blink Qt: `ProgressLine` in `mainwindow.py`.
11. **Address book checks after it is loaded,** one dialog after the other,
    each a list with check boxes, Select All / Deselect All and Ask Later
    (not asked again for the same contacts until they change, or the next
    start):
    - contacts in Deleted **and** in other groups (another client may leave
      one so): Restore, or Keep Deleted (out of the other groups);
    - contacts allowed to see the user's presence that the user does not
      subscribe to (not deleted, blocked, own, conference rooms or phone
      numbers only): Subscribe, or Stop Sharing (presence policy `block`, as
      unchecking "Allow to see my presence state" in the editor).
    The display rule (a deleted contact is shown in Deleted only) is here
    already (`isHiddenAsDeleted`).
    Blink Qt: `checklist_dialog`, `ContactModel._check_addressbook`.
12. **Deleted contacts.** The second line shows the contact's address, not its
    last message; Edit is offered (groups and presence subscription locked
    until it is restored); the Delete Permanently confirmation says how many
    messages go, and how many more of addresses other contacts list too.
    Blink Qt: `Contact.info`, `deleted_contact_menu`,
    `confirm_delete_permanently`, `MessageHistory.count_messages`.

### Low value

13. **One Activity log line per right click on a contact,** with the menu
    items offered (disabled ones in brackets). A debugging aid; cheap.

## Not to port

- The removal of the one-time copy of contact attributes to the shared XCAP
  bag: Blink Qt's own old namespace. (`preferred_media` is obsolete on every
  client.)
- Telling the GUI once per conversation per batch during a journal import:
  this client already refreshes once per page in bulk mode.
- Blink Qt bug fixes with no equivalent here (the observer decorator, the
  KeyError on Delete Permanently).

## Suggested order

8, then 1–7 as one series (the largest gain, and the clients then behave the
same on a new device), then 10, 11, 12, 9, 13.
