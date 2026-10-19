# Contact avatars and SylkServer account storage

Contact avatars are stored in SylkServer account storage. That is a private per-account object store on SylkServer, reached over HTTPS. Identity comes from SIP (the existing API token), and changes are announced by SIP MESSAGE. Avatars are the first use; the store can also hold other per-account client state that does not fit XCAP.

## Why
- Inline base64 avatars in the XCAP addressbook would inflate the single resource-lists document: 1–3 MB for a few hundred contacts, re-uploaded on every contact edit and re-fetched by every device.
- OMA pres-content is for publishing one's own icon to watchers, not for private avatars.
- So the image lives in account storage under the contact's id, and XCAP carries nothing about it. Clients cache it by ETag.

## Server side (SylkServer)

| File | Change |
|---|---|
| `sylk/applications/webrtcgateway/account_storage.py` | New. File and Cassandra backends, validation, ETags, quota |
| `sylk/applications/webrtcgateway/web.py` | `/storage/...` routes, change notification, admin purge also clears storage |
| `sylk/applications/webrtcgateway/sip_handlers.py` | `application/sylk-storage-changed` excluded from the journal (sender and receiver), but still delivered to the account's WebSocket clients |
| `sylk/applications/webrtcgateway/contact_avatars.py` | New. Avatars on the addressbook of WebSocket clients: attach on fetch, store/remove on contact update, delete with the contact, garbage collection |
| `sylk/applications/webrtcgateway/models/xcap.py` | `ContactAvatar`, `Contact.avatar` (never written to XCAP) |
| `sylk/applications/webrtcgateway/models/sylkrtc.py` | `avatars` (cached etags) on `account-fetch-addressbook` |
| `sylk/applications/webrtcgateway/addressbook.py` | `avatar` stripped from every contact written to XCAP |
| `sylk/applications/webrtcgateway/handler.py` | Fetch, xcap-diff refresh and contact update go through `contact_avatars` |
| `sylk/applications/webrtcgateway/configuration.py` | `[AccountStorage]` section |
| `sylk/applications/webrtcgateway/models/storage/cassandra.py` | `AccountObject` model |
| `tools/sylk-create-cassandra-tables` | Creates missing SylkServer Cassandra tables and brings existing ones to the schema of the models (dry-run by default, `--apply`) |
| `webrtcgateway.ini.sample` | Documentation and CQL |

### Configuration (`webrtcgateway.ini`, section `[AccountStorage]`)
| Setting | Default |
|---|---|
| `enabled` | `True` |
| `storage_dir` | `/var/lib/sylkserver/webrtcgateway/account_storage` (file backend only) |
| `max_object_size` | `262144` (256 KB) |
| `max_account_size` | `20971520` (20 MB) |
| `max_objects` | `10000` |

### Backends
- **Cassandra** is selected when `[Cassandra] cluster_contact_points` is set, as for message storage. It is safe with several SylkServers.
  - Table `account_storage`, one partition per account:
    ```sql
    CREATE TABLE IF NOT EXISTS account_storage (account text, namespace text, object_id text,
        etag text, size int, content_type text, modified bigint, device_id text, data blob,
        PRIMARY KEY (account, namespace, object_id));
    ```
  - SylkServer does not create or alter tables. Before deploying, run `sylk-create-cassandra-tables` (a dry run that prints the plan), then again with `--apply`.
    - The tool compares every table with the models in `models/storage/cassandra.py`. It creates missing tables, adds missing columns, drops columns the model lacks, and fixes TTL/gc_grace with `ALTER TABLE`, all keeping the rows.
    - A table whose key, column type or clustering order differs is recreated (DROP + CREATE, all rows lost), after a second confirmation.
    - An existing `account_storage` only gets `device_id` added.
    - Until the table matches, storage requests return 500; nothing else is affected.
  - Writes and deletes are conditional (LWT on etag), so a concurrent writer on another server gets 412.
  - The quota check is not atomic across servers, so it is a soft limit.
- **Files**: `<storage_dir>/<first char>/<account>/<namespace>/<id>`, with metadata in `.meta-<id>` and atomic writes. Single-server topology only.

### Logging
| Event | Level |
|---|---|
| Backend chosen (on first storage request) | info |
| Successful PUT and DELETE (account, ns/id, size) | info |
| Token failure | warning |
| Unexpected errors (500, e.g. table missing) | error |

Not logged: reads and the 400/404/412/413/507 rejections.

## Protocol

### Authentication and URL
- **Token:** the same API token and the same mechanism as the message journal fetch and file uploads. Nothing is specific to storage: every request carries `Authorization: Apikey <token>`, and a 401 is handled as for the journal.
- **URL:** derived from the same base as the journal and file transfers: `storage_url = <base>/webrtcgateway/storage/<account>`. The token reply also carries `storage_url`, but clients derive it from the server they are connected to and do not follow a URL found in a message.
- **WebSocket clients** (Sylk Mobile, sylk-desktop) use neither the token nor these URLs: their pictures travel on the addressbook contacts (see "WebSocket clients: avatars on the addressbook" below).
  - `<account>` is `user@domain`, lowercase, without `sip:`.

### Endpoints
| Method | Path | Result |
|---|---|---|
| GET | `{storage_url}` | `{account, namespaces:[{namespace, objects, size}], usage:{size, objects}, limits:{max_object_size, max_account_size, max_objects}}` |
| GET | `{storage_url}/<ns>` | `{account, namespace, objects:[{id, etag, size, content_type, modified, device_id}]}` (an empty list if the namespace does not exist) |
| GET / HEAD | `{storage_url}/<ns>/<id>` | Raw bytes, `Content-Type` as stored, `ETag: "<etag>"`, `X-Sylk-Device-Id: <uploader>` (if known), `Cache-Control: private, no-cache`. A matching `If-None-Match` returns 304 |
| PUT | `{storage_url}/<ns>/<id>` | Body is the raw bytes; `Content-Type` is stored. Returns 201 (created) or 200 (replaced) with JSON `{id, etag, size, content_type, modified, device_id}` and an `ETag` header |
| DELETE | `{storage_url}/<ns>/<id>` | 204 |

Rules:
- **Conditional PUT**:
  - `If-None-Match: *` creates only (412 if the object exists).
  - `If-Match: "<etag>"` replaces only if unchanged (412 otherwise).
  - With neither header the write is unconditional.
- **Conditional DELETE**: optional `If-Match: "<etag>"`.
- **Optional header `X-Sylk-Device-Id: <device id>`** on PUT and DELETE. Every device has a unique id. It is echoed in the change notification so the writing device can ignore its own change.
  - **On PUT it is stored with the object** as `device_id`, the uploader, and returned in the listing, the PUT response and the `X-Sylk-Device-Id` header of GET/HEAD. Together with `modified` (the upload time) it tells which device uploaded an object and when. Absent if the writer sent no header.
  - Stored in the object metadata (file backend: `.meta-<id>`; Cassandra: the `device_id` column of `account_storage`). Objects uploaded before this, or without the header, have none; clients show "unknown device".
- **Names**: `ns` and `id` must match `[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}` (no leading dot, no slash).
- **ETag**: the first 40 hex characters of the content's sha256.
- **`modified`**: milliseconds since the epoch, server time.
- **Content-Type**: parameters are dropped; an invalid or missing type becomes `application/octet-stream`.
- **CORS**: open (`*`). The `ETag` header is exposed.

### Errors
The body is `{"ok": false, "error": "..."}`.

| Code | Meaning |
|---|---|
| 400 | Invalid account, namespace or id |
| 401 | Missing or wrong token |
| 404 | No such object, or storage disabled |
| 412 | Precondition failed, or a concurrent write on another server |
| 413 | Object larger than `max_object_size` |
| 507 | Account quota (bytes or object count) exceeded |
| 500 | Server error (logged) |

### Change notification
After every successful PUT or DELETE, SylkServer sends a MESSAGE from `sip:<account>` to `sip:<account>`, so all registered devices get it, including the writer:
- `Content-Type: application/sylk-storage-changed`
- Body: `{"namespace": "avatars", "id": "...", "op": "put"|"delete", "etag": "...", "size": N, "content_type": "...", "modified": ms, "device_id": "..."}`
  - For a delete, the meta fields are those of the removed object.
  - `device_id` is present only if the writer sent `X-Sylk-Device-Id`.
- It is **not stored in the journal**, so an offline device never sees it and must resync at startup (see below).
- WebSocket clients receive it as an ordinary account `message` event (sender: the account itself). SylkServer must skip only the journal write for it: dropping it outright, as for `application/im-iscomposing+xml`, keeps it from every WebSocket client.
- It must not be shown, notified, or counted as unread.

### WebSocket clients: avatars on the addressbook
Sylk Mobile and sylk-desktop never talk to `/storage`. The gateway carries the pictures on the addressbook contacts it already sends them (`contact_avatars.py`). The avatar is never part of the XCAP document: the gateway adds it to what it sends and takes it out of what it writes.

**Fetch.** `account-fetch-addressbook` may carry `avatars: [{id, etag}]`, the pictures the client has cached.
- Present, even empty: every contact with a stored picture comes back with `avatar` = `{etag, contentType, size, modified, deviceId}`, plus `data` (base64) only where the client's etag differs. Contacts without a picture have no `avatar`.
- Absent: no avatars, as before. Older clients are unaffected.
- The gateway remembers, per connection, which etags the client holds. Refreshes it pushes on its own (xcap-diff) carry avatars the same way.
- **Garbage collection** runs here: an object with no contact, older than 7 days, is deleted. Not on an empty document, which may be a failed fetch.

**Update.** `account-update-addressbook` for a contact may carry:
- `avatar: {data, contentType, deviceId}` to store a picture;
- `avatar: {remove: true, deviceId}` to delete it.

Any other `avatar` (the metadata a client echoes back) is ignored. The picture is stored after the XCAP write succeeded. The `addressbook-updated` echo, sent to every device of the account, carries:
- the stored avatar **with its data**, so the other devices update without a fetch;
- `{code: 204}` after a removal;
- `{code, error}` when storing failed (413, 507, 500).

Deleting a contact deletes its picture. Every change is announced with `application/sylk-storage-changed`, as for the HTTP routes, so Blink sees it.

**Changes made over HTTP** (Blink) reach WebSocket clients as `application/sylk-storage-changed`. A `put` with an etag the client does not have makes it fetch the addressbook again, which brings the data. A `delete` drops the cached picture.

### Testing with curl
```sh
B=https://<server>/webrtcgateway/storage/alice@example.com
H="Authorization: Apikey <token>"
curl -s -H "$H" $B
curl -s -H "$H" -X PUT -H 'Content-Type: image/webp' -H 'If-None-Match: *' --data-binary @a.webp $B/avatars/0f3c...
curl -s -H "$H" $B/avatars
curl -s -H "$H" -o out.webp -D - $B/avatars/0f3c...
curl -s -H "$H" -X DELETE -H 'If-Match: "<etag>"' $B/avatars/0f3c...
```

## Avatars (client convention)

### Image
- Square centre crop, **96×96** pixels (not points), metadata stripped.
- **WebP** at quality ~75, or JPEG at quality ~70 (4:2:0), about 2–4 KB each. Blink Cocoa writes JPEG (AppKit has no WebP encoder).
- Encode consistently across clients. Decoding must accept both WebP and JPEG.
- Sylk Mobile encodes in JavaScript (jpeg-js), which writes 4:4:4 chroma: a few hundred bytes more at 96×96, readable everywhere.

#### Building the avatar
Every client builds the uploaded bytes the same way:
1. **Crop:** take the largest centred square of the source: `side = min(w, h)`, origin `((w - side) / 2, (h - side) / 2)`. The contact editor already lets the user frame a square; the centre crop then changes nothing.
2. **Scale:** draw that square into a new **96×96** bitmap with high-quality interpolation. Drawing into a fresh bitmap is what strips every piece of metadata (EXIF, GPS, colour profile names, thumbnails).
3. **Flatten:** fill the bitmap white first, so transparent sources come out on white rather than black. JPEG has no alpha.
4. **Encode:** JPEG at quality 0.70 (or WebP at ~75). Upload with `Content-Type: image/jpeg` (or `image/webp`).

Blink Cocoa (`AccountStorage.encode_avatar`, main thread):
```python
AVATAR_PIXELS = 96
AVATAR_JPEG_QUALITY = 0.70

def encode_avatar(image):            # NSImage -> bytes, or None
    size = image.size()
    if size.width <= 0 or size.height <= 0:
        return None
    px = AVATAR_PIXELS
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, px, px, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
    rep.setSize_((px, px))                       # 96 points == 96 pixels, no Retina doubling
    context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    try:
        NSGraphicsContext.setCurrentContext_(context)
        context.setImageInterpolation_(NSImageInterpolationHigh)
        NSColor.whiteColor().set()
        NSRectFill(NSMakeRect(0, 0, px, px))     # flatten onto white
        side = min(size.width, size.height)      # centre square crop
        source = NSMakeRect((size.width - side) / 2.0, (size.height - side) / 2.0, side, side)
        image.drawInRect_fromRect_operation_fraction_(NSMakeRect(0, 0, px, px), source,
                                                     NSCompositeSourceOver, 1.0)
        context.flushGraphics()
    finally:
        NSGraphicsContext.restoreGraphicsState()
    data = rep.representationUsingType_properties_(NSJPEGFileType,
                                                   {NSImageCompressionFactor: AVATAR_JPEG_QUALITY})
    return bytes(data) if data is not None else None
```

Equivalent with Pillow, for Blink Qt or tools:
```python
from PIL import Image, ImageOps
import io

def encode_avatar(path):
    with Image.open(path) as source:
        source = ImageOps.exif_transpose(source)               # honour the camera rotation
        image = ImageOps.fit(source.convert('RGBA'), (96, 96), Image.LANCZOS, centering=(0.5, 0.5))
    flat = Image.new('RGB', image.size, (255, 255, 255))
    flat.paste(image, mask=image.getchannel('A'))              # flatten onto white
    out = io.BytesIO()
    flat.save(out, 'JPEG', quality=70, subsampling='4:2:0', optimize=True)   # no exif= -> no metadata
    return out.getvalue()
```

### Object id
- The object id is the **contact id**: one object per contact, `avatars/<contact id>`. No separate avatar id.
  - Contact ids already match the name rule (`id` + digits). A contact whose id does not keeps its avatar local only.
- **Nothing is written to XCAP.** Whether a contact has a picture, and which version, is what the storage says. The namespace listing gives every id with its etag in one request, and the change notification covers the time in between.
  - No XCAP write per picture change, no addressbook-update tick, and no race between the object and an XCAP reference.
- Changing the picture: `PUT avatars/<contact id>` (unconditional, last writer wins), replacing the object in place.
- Removing the picture: `DELETE avatars/<contact id>`.
- Permanently deleting a contact (expunge from Deleted) also deletes its object. If that fails, garbage collection removes it later.

### Local cache
- Keyed by contact id, storing `{etag, content_type, bytes}` per account.
- It mirrors the server: a contact has a stored picture exactly when the cache holds one under its id. Displaying never makes a request.
- A 404 means the object is gone: drop it and show the default picture. Do not retry until the next listing or notification.

### Pictures set on this device
On every sync, each picture the user set on this device (in Blink Cocoa: set in the contact editor, `icon_info.local`; in Blink Qt: `alternate_icon`) that was never uploaded (not pending) is reconciled. Presence and bundled icons never count: on the server they would be a copy that goes stale.

**In steady state there is never anything to choose.** A picture set in the editor is pending until its upload succeeds, and the local copy is dropped then. A change from another device only updates the cache. So with every device in sync, changing an avatar on one device is last writer wins, and nobody is asked.

**A choice is offered only at the first sync of an account on a device.** The first sync is when pictures from before account storage are met. After it, a device keeps a per-account marker, `first_sync_done`, in its local state (not in XCAP, not on the server).
- The marker is set once the listing has been read and either nothing needed a choice or the user has confirmed it.
- If the listing fails, or the app quits before Confirm, the marker stays unset and the next sync starts the first sync again.

**First sync (no marker):**
- **Nothing on the server:** the local picture is pushed. This is how pictures from before account storage get uploaded.
- **A picture on the server too:** the user chooses. The server copies are fetched first, so both can be shown.
  - A modal lists one row per contact with two columns, **On the server** and **On this device**. Each shows the picture, a **Keep** tick box, and where it comes from: the uploader (this device, or another device by its id) and the upload time (`modified`) for the server; the file's time for the local one.
  - In each row exactly one box is ticked. Ticking one unticks the other. The default is the newer picture.
  - **Select All** above each column ticks that whole column. **Confirm** is the only way out.
  - Keeping the server picture deletes the local one. Keeping the local picture uploads it, replacing the server copy for all devices.
- Either way the local file goes away (after a successful upload, or when the server copy is kept), so each contact is handled once. If the app quits before Confirm, the next sync asks again.

**Every later sync (marker set):** a user-set picture that was never uploaded is newer than whatever the server holds. It was set while the storage was not reachable, or before the storage account was known (discovery, first token). It is marked pending and uploaded, replacing the server copy, without asking.

A picture with an upload or delete pending is left to that retry in both cases.

**Phonebook photos** (Sylk Mobile; any client with a system address book). The photo the phone's address book has for a contact is a picture of this device, like one set in the editor, and goes to the server by the same rules. The presence icon is only what Blink publishes in its presence document; a phonebook photo is not one.
- Only contacts on the server, not the own contact, and only photos the device can read (no remote URLs, so no gravatars).
- A picture set in the editor wins over the phonebook photo of the same contact.
- Each photo version is taken in once: the device keeps a hash of the source per contact. Once that version is uploaded, kept against the server picture in the choice, refused for size, or overridden by a choice or removal in the editor, it is not taken in again. A changed phonebook photo is a new version, newer than the server copy, and is uploaded.
- The phonebook has its own first-pass marker, `phonebook_first_done`, set only once the phonebook has been read or cannot be read (no permission). Until then the first sync is not finished. A device that synced before phonebook photos counted therefore still gets the choice once for contacts that have a server picture and a phonebook photo.

### Sync
After the XCAP addressbook has been fetched and is stable (same rule as the journal), and again on re-registration (at most every 10 minutes) to catch notifications missed while disconnected:
1. `GET {storage_url}/avatars`
2. For each object whose id is a contact id: if it is missing from the cache or has a different etag, GET it.
3. Drop cached ids that are no longer on the server, or whose contact is gone.
4. **Garbage collection**: an object whose id is not a contact id is deleted once it is older than 7 days (`modified`), with `If-Match` on its etag. The age limit protects a fresh upload whose contact has not yet reached this device's addressbook.
- Contacts with an upload or a delete still pending on this device are skipped, so the old picture does not overwrite the new one and a removed picture does not come back.
- The re-list never runs before the addressbook has settled: the garbage collection decides on which contacts exist.

### Live sync
On `application/sylk-storage-changed` with `namespace == "avatars"`:
- Ignore it if `device_id` is our own.
- `op == "put"`: if the cached etag differs, GET it. This applies even when the contact has not arrived yet; an orphan is dropped by the next sync.
- `op == "delete"`: drop the id from the cache and refresh the views showing it.

### Upload (contact editor)
1. Crop and scale the picture, then encode it. Keep it locally and mark the upload as pending, so it shows at once.
2. `PUT {storage_url}/avatars/<contact id>` with `X-Sylk-Device-Id`.
3. On 200 or 201: cache the bytes under the contact id, clear the pending mark, and drop the local copy.
4. Errors:
   - 413 or 507: tell the user (size or quota), clear the pending mark, and drop the rejected local picture (and its user-set flag). The contact keeps its old avatar, the stored one or else the presence icon, and the rejected picture is not pushed again by the next sync.
   - 401: handled by the common token mechanism, as for the journal.
   - Network error: keep the picture locally and pending, and retry later (when the addressbook settles, when a new token arrives, and every 5 minutes).
- A removal whose DELETE fails stays pending the same way and is retried.

### Accounts without SylkServer
Avatars stay local only, as before.

### Display
A picture the user set has priority. Without one, the icon learned from presence is shown. Without that, the synthetic avatar (initials on a colour derived from the name).
1. A picture the user just set that is still waiting to be uploaded.
2. The stored picture. Only user-set pictures are ever uploaded; presence icons never go to the server.
3. The local picture: a user-set picture on an account without account storage, otherwise the presence icon.
4. Initials.

Removing the picture (here or on another device) therefore falls back to the presence icon, if the contact publishes one.

The contact list, contact editor and Alert panel (incoming call) show the same avatar, from the same cache.

## Open decisions
- **Encrypting avatars** with the account's own PGP key (Content-Type `application/pgp-encrypted`), so the server operator cannot see them.
  - Costs a decrypt per avatar, and the key must be present before avatars can be shown.
  - Not decided. Start unencrypted; the content type tells clients which form they got.
- **Own avatar**: stored the same way on the own contact, or also published via presence? Not decided.
- **Other namespaces** (preferences, drafts, group metadata): possible later.

## Implementation order
1. Blink Cocoa.
2. Blink Qt, ported from Cocoa so both behave the same.
3. Sylk Mobile and sylk-desktop.

## Client requirements
What every client implements, independently of its toolkit. Blink Qt should match Blink Cocoa on each point.

### Which account
- The addressbook is replicated to every XCAP account, but an avatar object lives in one storage: the **storage account**.
- Storage account: the default account when it is enabled, has XCAP enabled and has a SylkServer (a storage URL can be derived); otherwise the first account that qualifies. None: avatars stay local only.
- Uploads, deletes, listings and notifications use the storage account only. A notification for another account is ignored.

### Device id
- `X-Sylk-Device-Id` is the device's own instance id: the bare uuid, without `urn:uuid:`, the same id the client uses everywhere else.
- It is how a device recognises its own changes in notifications, and how the UI names the uploader: "This Mac" / "This computer" / "This device" for our own id, otherwise "Device" plus the first 8 characters of the id.
- Sylk Mobile uses its existing device id (`getUniqueId()` of react-native-device-info: the Android id, or the iOS identifier for vendor), the one it already sends as `deviceId` with push tokens and message metadata. It is not a uuid on Android; the server stores the header as given.

### Per-contact local state
Never written to XCAP:
- **user-set picture**: a flag saying the local picture was set by the user (Blink Cocoa: `icon_info.local`).
  - Set when the user picks a picture in the editor. Cleared after a successful upload, and when the user removes the picture.
  - Presence must not overwrite the local picture while it is set.
- **avatar_pending**: a user-set picture not uploaded yet.
- **avatar_delete_pending**: a removed picture whose DELETE has not succeeded yet.
- **first_sync_done** (per account, not per contact): this device has been through its first avatar sync with the account (see "Pictures set on this device"). Blink Cocoa and Blink Qt: `account_storage/<account>/state.json` (`AccountStorage.first_sync_done`, set by `_first_sync_finished`). Sylk Mobile: `state.first_sync_done` in the avatar `index.json`.
- The local picture file is shared between a user-set picture and the presence icon. The flag says which one it is.

### Contact editor
- Only an explicit action in the photo well counts as a change: choosing a picture (file panel, drag and drop) or clearing it.
- Saving a contact whose well shows a presence icon must not upload it. Do not detect a change by comparing images.
- New picture: save it locally, set user-set and pending, save the contact, then upload after the addressbook transaction.
- Picture cleared: delete the local file, clear user-set, set delete-pending, drop the cache entry at once, then DELETE.

### When things run
- **Sync** (listing, fetch, drop, GC, push, and the choice on the first sync only): when the XCAP addressbook has settled after a fetch (the same point the journal and call history wait for), or when the storage account's first token arrives after that. Again on re-registration, at most every 10 minutes, and only after the first settle, because GC decides on which contacts exist.
- **Pending retries** (uploads and deletes): on settle, when a new token arrives, and every 5 minutes.
- **Expunging a contact** (permanent delete from the Deleted group): DELETE its object, best effort; GC covers a failure.
- All HTTP off the GUI thread, serialised on one worker. Contact and UI updates back on the GUI thread.
- Only one choice window at a time. If another modal is open, wait for it to close.

### `application/sylk-storage-changed`
- Handled at the very start of incoming MESSAGE processing, before anything stores, displays, notifies or counts it, the same way as other control types.
- Skipped if it ever appears in a journal replay.
- Listed among the control content types, so it is never rendered or acknowledged.

### Cache
- Private to the device, layout free. Blink Cocoa: `<app data>/account_storage/<account>/avatars/<contact id>` plus an `index.json` with `etag`, `content_type`, `size`, `device_id` and `modified` per id.
- Showing a contact never makes a request; only sync, notifications and uploads change the cache. Views refresh when an id in the cache changes.

### Notifications to the user
- 413: "Contact picture not saved — The picture is too large for the server".
- 507: "Contact picture not saved — The account storage on the server is full".
- Nothing else is surfaced. Network errors are retried silently.

### Logging
Every line starts with `[avatar]`, so `grep '\[avatar\]'` gives the whole story. It includes:
- storage URL in use (once);
- per sync: counts on server / to fetch / dropped / orphans deleted;
- **one line per contact uploaded**: name, URI, id, reason (`set in the contact editor`, `set on this Mac/computer, none on the server`, `kept over the server picture`, `set on this Mac/computer, not uploaded yet`, `retry`), size, etag;
- the first sync of an account done (once per account per device);
- one line per failed upload (413 / 507 / postponed), per delete, and per remote change applied;
- the number of contacts in a choice and the totals chosen.

## Blink Cocoa implementation
Reference for the port.

| Requirement | Where |
|---|---|
| Storage URL, HTTP client, cache, encoding, sync, GC, push, live sync, upload, delete, logging | `AccountStorage.py` |
| Server-or-local choice window | `AvatarConflictPanel.py` |
| `avatar_pending`, `avatar_delete_pending` (local settings) | `configuration/contact.py` |
| Display precedence | `ContactListModel.py`, `PresenceContactAvatar.from_contact` |
| Add/edit contact upload or remove, expunge delete, view refresh on `BlinkContactAvatarDidChange` | `ContactListModel.py` (`addContact`, `editContact`, `expungeContacts`) |
| Editor: explicit change flag (`photoChanged` → `icon_changed`), uploader tooltip | `ContactController.py` |
| Presence must not overwrite a user-set picture | `ContactListModel.py`, presence icon handling checks `icon_info.local` |
| `application/sylk-storage-changed` intake, journal skip | `SMSWindowManager.py`; control types in `SMSViewController.py` |
| Settle trigger | `BlinkAddressbookDidSettle` (posted by `ContactListModel` after the post-reload pass) |

## Blink Qt implementation

| Requirement | Where |
|---|---|
| Storage URL (the file transfer base minus `/filetransfer`), HTTP client (`requests`, `tls.verify_server`), cache, encoding (QImage: centre crop, 96×96, flattened onto white, JPEG 70), sync, GC, push, live sync, upload, delete, logging | `blink/account_storage.py` |
| Server-or-local choice window | `blink/avatar_choice.py` (`AvatarChoiceDialog`) |
| `avatar_pending`, `avatar_delete_pending` (local settings) | `blink/configuration/addressbook.py` |
| Display precedence | `account_storage.contact_picture`, used by `Contact.icon` and `ContactDetail.icon` in `blink/contacts.py` and by the editor's picture well |
| Add/edit contact upload or remove, delete-permanently DELETE | `blink/contacts.py` (`ContactEditorDialog._SH_Accepted`, `ContactTrash.delete_permanently`) |
| Editor: explicit change (`IconSelector.filename` is `NotSelected` unless a picture was chosen or cleared), uploader tooltip | `blink/widgets/labels.py` (`IconSelector`) |
| Presence must not overwrite a user-set picture | Already the case: the user-set picture is `images/<id>_alt.png` (`contact.alternate_icon`), the presence icon `images/<id>.png` |
| `application/sylk-storage-changed` intake | `MessageManager._NH_SIPEngineGotMessage` in `blink/messages.py`, before anything is logged as a message, stored or shown |
| Journal skip | `STORAGE_CHANGED_CONTENT_TYPE` in `blink/message_envelopes.py`, in `IGNORED_CONTENT_TYPES` (`blink/journal.py`) and `__not_history_content_types__` |
| Settle trigger | `BlinkAddressbookDidSettle` (posted by `ContactRepair`, `data.accounts`); also the first token of the storage account if it arrives after the settle |
| View refresh | `BlinkContactAvatarDidChange` posted with the addressbook contact as sender, so `Contact`, `ContactDetail` and the open contact editor redraw |
| Start | `AccountStorage().start()` in `blink/__init__.py` |

Qt specifics:
- **"User-set" flag:** `contact.alternate_icon` together with the `images/<id>_alt.png` file. It is cleared, and the file removed, after a successful upload or when the server picture is kept in the choice window.
- **Removing in the editor:** "Use contact provided icon" removes the picture and sends the DELETE. The well then shows the presence icon, if any.
- **First sync marker:** `first_sync_done` in `account_storage/<account>/state.json`, set by `_first_sync_finished` after a sync with nothing to choose, or after the choice is confirmed.
- **Device label:** "This computer" for our own id.
- **Notifications:** 413 and 507 are told through `org.freedesktop.Notifications`, or through a non-modal message box when there is no notification service.

## Sylk Mobile implementation

| Requirement | Where |
|---|---|
| Cache, display, reconcile with the fetched addressbook, first-sync marker, choice, upload and removal through contact updates, live changes, logging | `app/accountStorage.js` (`AvatarStore`): no React Native imports; file system, transport, clock and app callbacks are injected, so it runs under node |
| File system for it (RNFS), the shared instance, view hooks | `app/avatars.js` (`avatarPhotoFor`, `avatarInfoFor`, `useAvatarVersion`, `setContactResolver`) |
| Encoding (EXIF orientation, centre crop, 96×96 area average, flattened onto white, JPEG 70) | `app/avatarImage.js` (`encodeAvatarBase64`), pure JavaScript: jpeg-js, and pako for PNG |
| Etags on the fetch, picture data taken off the contacts into an `avatars` event, avatar metadata kept across XCAP-only writes | react-native-sylkrtc `Addressbook.setAvatarCache`, `_takeAvatars`, `_keepAvatar`; `Account._fetchAddressbook` (`patches/react-native-sylkrtc+2.1.0.patch`) |
| Server-or-local choice window | `app/components/AvatarConflictModal.js` |
| Display precedence | `UserIcon` (every avatar in the app goes through it), `AvatarOrGravatar` in `UserIcon.js` for the contact list and the chat header |
| Editor: explicit change only, uploader caption | `EditContactModal.js` (`avatarChange`: tap the picture or *Add/Change picture* to pick from the library, *Remove* to clear; applied only on Save) |
| Editor save | `saveContactByUser` → `_applyAvatarChange` (after `saveSylkContact`, when the row has its final `contact_id`) |
| Upload and removal | `_avatarSend`: `updateContact({...serverContact, avatar})` through `_abExecWhy('avatar', …)`, with no addressbook-update tick (the echo reaches the other devices) |
| Contact deleted | the gateway deletes the picture; `_deleteSylkContactImpl` drops the local copy (`avatarStore.expunge`) |
| `application/sylk-storage-changed` intake | first thing in `incomingMessageFromWebSocket` → `_handleAccountStorageMessage`; it and `application/sylk-api-token` (Blink's, forked to every device) are also dropped in `outgoingMessage`, `handleIncomingMessage`, `incomingMessageFromPush`, the journal replay, `outgoingMessageFromJournal` and `saveOutgoingMessageSql` |
| Refetch (missing data, a change made over HTTP) | `_avatarRefetch`, through the addressbook-update `FetchScheduler` (jittered, throttled) |
| Settle trigger | the end of `_abRunReconcile`, after a pass that applied a freshly fetched document with no rerun pending → `_avatarOnAddressbookSettled` → `reconcile(ab.contacts)` |
| Retries | on settle, on `registered`, and every 5 minutes; an upload whose echo does not come within 60 s is sent again |
| Account switch, logout | `_avatarBindAccount` (from `componentDidUpdate`), `avatarStore.reset()` in the account teardown |

Mobile specifics:
- **Storage account:** the current account (mobile has one at a time), when the server publishes `addressBookServer`. Otherwise pictures set in the editor stay on the device.
- **No token, no HTTP:** everything goes over the authenticated WebSocket, on the addressbook (see "WebSocket clients").
- **Sync** is a comparison, not a request: on every settle the cache is made equal to the `avatar` metadata of the fetched contacts. A picture whose data did not come (the gateway's memory of our etags was wrong) triggers a refetch, at most every 10 minutes.
- **Contact id:** the row's `remote_id`, the XCAP contact id. A picture set before the contact reached the server stays pending until it has one.
- **Per-contact local state** is not in the contacts table: it lives in the avatar index, keyed by the local `contact_id`, as `{file, userSet, pending, setAt}` or `{deletePending, remoteId}`.
- **Cache:** `<Documents>/account_storage/<account>/avatars/`, one file per object named `<id>.<etag>.<ext>` (a new name per version, so image views never show a stale copy), plus `index.json` (with `state.first_sync_done`). File names, not paths, are stored: the iOS container path changes on every update.
- **Phonebook photos go to the server** (see "Phonebook photos" above). The store reads them through `readPhoto` (RNFS: `content://` on Android, the Caches file on iOS, `data:` uris), encodes them with `encodeAvatarBase64`, and keeps them in the index as `{file, userSet, source: 'phonebook', srcHash}`. The pass runs on every settle and whenever `_abApplyPhotosToContacts` re-applies the phonebook. Each photo is read once per run; only a new version is encoded. The phonebook counts as ready after `_abAdoptPhonebookNames`, read or not (`_abPhonebookChecked`).
- **Gravatars** are shown when a contact has no picture at all, and never uploaded.
- **Choice window:** the local column says *Phonebook* or *This device*.
- **Picking:** react-native-image-picker shrinks the photo to 512 px first (decoding a full camera frame in JavaScript would take seconds). On Android it does not rotate the pixels, it copies the EXIF orientation, which the encoder applies.
- **The own contact** is not offered a picture (see Open decisions).
- **Choice window:** opened only when no other modal or call is on screen and the app is in the foreground; otherwise tried again every 5 seconds.
- **Notifications:** 413 and 507 through the in-app notification banner.
- **Dependencies:** `jpeg-js` and `pako` in `package.json`.
