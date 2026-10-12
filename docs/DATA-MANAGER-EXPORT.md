# Data manager: Export to device

Blink for macOS can export its message history to another device of the same
account: Sylk Mobile, or Blink on another computer. It speaks the export API
of Sylk Mobile (sylk-mobile `docs/export/Readme.md`), so the phone's own
Import screen works against Blink unchanged, and so does Blink's Import from
device.

Blink > Data Manager... > **Export to device**. The Data Manager's sections are
Local Messages, File transfers on server, Import from device and Export to device.

## Flow

1. Choose the account and press **Start Export**.
2. Blink reads the account's history once, starts an HTTP server on the
   local network (IPv4, random port 49152–65535) and announces it to the
   account's own devices.
3. Each of those devices that is online decrypts the announcement and, when
   it is less than 60 seconds old, opens its Import screen. **Announce
   Again** repeats the announcement for a device that was off or asleep.
4. On that device, choose what to copy and import it. The section shows
   which device is importing, how many requests it made and the last one.
5. **Stop Export**, or closing the Data Manager window, stops the server.
   Quitting Blink stops it too.

## Security

* **Announcement**: `application/sylk-data-export`, payload
  `{v, server, key, enc, timestamp}`, PGP-encrypted to the account's own
  public key and sent to the account itself. It carries
  `X-Sylk-Skip-Journal`, since a replay would be stale anyway. Blink ignores
  the copy of its own announcement that comes back to it.
* **Token**: 8 characters, Crockford base32, accepted as `?token=` or
  `Authorization: Bearer`. Nothing but `/`, `/api/ping` answers without it.
* **Encryption**: a fresh 32-byte NaCl secretbox key per export, sent only
  inside the PGP announcement. A client sending `X-Sylk-Enc: 1` (the phone's
  import and Blink's) gets every body as `nonce(24) || box` with
  `X-Sylk-Enc: 1`. Without the header, answers are plaintext, as the phone
  does for browsers.
* The server runs only while the Data Manager window is open.

The account needs its PGP key on this computer (`keys/<account>.pubkey`);
without it Start Export is disabled and the section says why. Bonjour is not
offered.

## What is exported

A snapshot taken at Start: the account's live rows whose category is `text`
or `location`, and its file transfers. Calls, metadata sidecars and other
rows are not exported (the phone does not export them either). Rows added
after Start are not in it; stop and start again to include them.

Each row is shaped as a row of the phone's `messages` table: `msg_id`,
`timestamp` (JSON-quoted ISO time, as the phone stores it),
`unix_timestamp`, `content`, `content_type`, `metadata`, `from_uri` and
`to_uri` (the account and the other party, without `sip:`), `direction`,
`state`, `sent`, `received`, `related_msg_id`, `related_action`, `category`,
`has_link` and `encrypted`. For `encrypted`, 1 means the body is still PGP
ciphertext and the importing device decrypts it; 2 means it was decrypted
here; 0 means it was never encrypted.

**Files** are offered with the bytes Blink has: the decrypted file in the
transfer cache, found under any account folder as the cache itself looks.
The row's `content` and `metadata` therefore name the file without `.asc`
and carry no `.asc` URL, or the phone would take the plaintext for
ciphertext; other envelope fields are kept. A transfer whose file is not on
this computer is still offered: its row imports and `/api/blob` answers 404,
as on the phone.

**Contacts** are not exported. They sync through the server's addressbook,
so the contacts kind is empty and `/api/contacts.json` returns `[]`.

## API served

| Path | Answer |
|---|---|
| `/` | a one-line page saying this is Blink exporting (no browser UI, no zip) |
| `/api/ping` | `{"ok":true}`, no token needed |
| `/api/summary` | device (`useragent` is Blink's SIP user agent, which the phone stamps as the rows' origin), counts per category, file count and bytes |
| `/api/calendar?contact=` | per-category day index, plus `all` and `links` |
| `/api/ids?kind=&category=&period=&contact=` | the msg_ids of a selection |
| `/api/rows-bulk?...` | the rows of a selection |
| `/api/idindex?kind=&category=&contact=` | `{id, day, contact}` for every row of the kind |
| `/api/contacts-counts?kind=&category=&period=` | the contact train, most first |
| `/api/meta?id=` | one row |
| `/api/blob?id=` | one file's bytes |
| `/api/contacts.json` | `[]` |

`kind` is `messages` or `files`. `category` is `all`, `text`, `links` or
`location` for messages, and `all`, `image`, `audio`, `video` or `other` for
files. `period` is `all`, `YYYY`, `YYYY-MM` or `YYYY-MM-DD`, using the local
day as the phone does.

## Code

| File | Role |
|---|---|
| `DataExport.py` | the snapshot (rows shaped for the phone) and the HTTP server; no AppKit |
| `DataExportPanel.py` | the Export to device section |
| `DataImport.py` | `secretbox_seal` and `new_secretbox_key`, next to `secretbox_open` |
| `HistoryManager.py` | `ChatHistory.data_export_rows` |
| `SMSWindowManager.py` | `sendDataExportAnnouncement`; ignores the echo of our own export (`DataExport.ACTIVE_TOKENS`) |
| `DataManagerWindow.py` | the fourth section; stops the export when the window closes |

## Verified

Outside the application, against a fake history and transfer cache:

* `secretbox_seal`: PyNaCl and the pycryptodome fallback produce identical
  boxes, and `secretbox_open` reverses them.
* Blink's own `DataImport.ImportClient`, with sealed answers: summary,
  calendar, ids per category, period and contact, rows, meta, blob (also from
  another account's cache folder), 404 for a missing file, 401 without the
  token, and a plaintext answer without `X-Sylk-Enc`.
* The phone's real `app/ImportClient.js` and `app/ExportCrypto.js`
  (tweetnacl), run under Node: summary, calendar, contact train, id index,
  preview diff, message import and file import.

Not yet tested in the application: the window itself, the announcement over
SIP, and a real phone on the same Wi-Fi.

## Testing

1. Open Data Manager > Export to device, pick the account, press Start
   Export. The section shows the address, the key and the counts, and the log
   has `[export] Exporting <account> at http://...`.
2. With Sylk Mobile open on the same Wi-Fi, its Import screen opens within
   a few seconds. If it does not, press Announce Again with the app in the
   foreground.
3. Import some text messages, then some pictures. They appear in the phone's
   conversations; pictures open without being fetched from the server.
4. While the phone imports, the section names its address and counts its
   requests.
5. Close the Data Manager window: the phone's import reports the connection
   lost, and the log has `[export] Export of ... stopped`.
6. Blink to Blink: start the export on one Mac; the other opens Import from
   device by itself.
7. An account without its PGP key here: Start Export is disabled, with the
   reason shown.
