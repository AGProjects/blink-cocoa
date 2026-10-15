# Contact avatars and SylkServer account storage

Contact avatars are stored in SylkServer account storage. That is a private per-account object store on SylkServer, reached over HTTPS. Identity comes from SIP (the existing API token), and changes are announced by SIP MESSAGE. Avatars are the first use; the store can also hold other per-account client state that does not fit XCAP.

## Why
- Inline base64 avatars in the XCAP addressbook would inflate the single resource-lists document: 1–3 MB for a few hundred contacts, re-uploaded on every contact edit and re-fetched by every device.
- OMA pres-content is for publishing one's own icon to watchers, not for private avatars.
- So the XCAP contact carries only a short reference. The image lives in account storage, fetched lazily and cached by ETag.

## Server side (SylkServer)

| File | Change |
|---|---|
| `sylk/applications/webrtcgateway/account_storage.py` | New. File and Cassandra backends, validation, ETags, quota |
| `sylk/applications/webrtcgateway/web.py` | `/storage/...` routes, change notification, admin purge also clears storage |
| `sylk/applications/webrtcgateway/sip_handlers.py` | `application/sylk-storage-changed` excluded from the journal (sender and receiver) |
| `sylk/applications/webrtcgateway/configuration.py` | `[AccountStorage]` section |
| `sylk/applications/webrtcgateway/models/storage/cassandra.py` | `AccountObject` model |
| `tools/sylk-create-cassandra-tables` | Creates missing SylkServer Cassandra tables (dry-run by default, `--apply`) |
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
        etag text, size int, content_type text, modified bigint, data blob,
        PRIMARY KEY (account, namespace, object_id));
    ```
  - SylkServer does not create tables. Create it before deploying with `sylk-create-cassandra-tables --apply`, or with the CQL above. Until it exists, storage requests return 500; nothing else is affected.
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
1. Get the API token as today. Send a MESSAGE with `Content-Type: application/sylk-api-token` to the server. The reply is a MESSAGE of the same type with body `{"token": "...", "url": "<base>/webrtcgateway/messages/history/<account>"}`.
2. **Derive** the storage URL from the same base as file transfers: `storage_url = <base>/webrtcgateway/storage/<account>`. The server does not announce it.
   - `<account>` is `user@domain`, lowercase, without `sip:`.
3. Every request carries `Authorization: Apikey <token>`.
   - On 401, request a new token once and retry.
   - The token has a TTL of 31 days and is refreshed whenever re-requested.

### Endpoints
| Method | Path | Result |
|---|---|---|
| GET | `{storage_url}` | `{account, namespaces:[{namespace, objects, size}], usage:{size, objects}, limits:{max_object_size, max_account_size, max_objects}}` |
| GET | `{storage_url}/<ns>` | `{account, namespace, objects:[{id, etag, size, content_type, modified}]}` (an empty list if the namespace does not exist) |
| GET / HEAD | `{storage_url}/<ns>/<id>` | Raw bytes, `Content-Type` as stored, `ETag: "<etag>"`, `Cache-Control: private, no-cache`. A matching `If-None-Match` returns 304 |
| PUT | `{storage_url}/<ns>/<id>` | Body is the raw bytes; `Content-Type` is stored. Returns 201 (created) or 200 (replaced) with JSON `{id, etag, size, content_type, modified}` and an `ETag` header |
| DELETE | `{storage_url}/<ns>/<id>` | 204 |

Rules:
- **Conditional PUT**:
  - `If-None-Match: *` creates only (412 if the object exists).
  - `If-Match: "<etag>"` replaces only if unchanged (412 otherwise).
  - With neither header the write is unconditional.
- **Conditional DELETE**: optional `If-Match: "<etag>"`.
- **Optional header `X-Sylk-Device-Id: <device id>`** on PUT and DELETE. It is echoed in the change notification so the writing device can ignore its own change.
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
- It must not be shown, notified, or counted as unread.

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
- Square centre crop, **96×96**, metadata stripped.
- **WebP** at quality ~75, or JPEG at quality ~70 (4:2:0), about 2–4 KB each.
- Encode consistently across clients. Decoding must accept both WebP and JPEG.

### Reference in XCAP
- The contact gets a shared (XCAP-replicated) attribute **`avatar = <id>`**.
- `<id>` is a fresh random uuid4 hex per upload. Never a content hash, so paths leak nothing about the image.
- Changing the picture:
  1. upload under a new id;
  2. update the contact's `avatar` attribute;
  3. delete the old object.

  Using a new id instead of overwriting avoids races with devices that still reference the old one.
- Removing the picture: clear the attribute, then delete the object.

### Local cache
- Keyed by `id`, storing `{etag, content_type, bytes}` per account.
- On display: if `avatar` is set and the id is not cached, GET it.
- Cached avatars are shown without a network request. They are refreshed only by notifications or the startup diff.
- A 404 means the object is gone: show the default picture and do not retry until the next startup diff or notification.

### Startup sync
After the XCAP addressbook has been fetched and is stable (same rule as the journal):
1. `GET {storage_url}/avatars`
2. For each id referenced by a contact:
   - missing from the cache, or with a different etag → GET it;
   - absent on the server → treat as no avatar.
3. Drop cached ids that no contact references.
4. **Garbage collection** of server objects that no contact references:
   - Proposed: a device deletes an unreferenced object only if it is older than 7 days (`modified`).
   - It runs after a stable addressbook fetch. The age limit protects uploads whose XCAP update has not propagated yet.

### Live sync
On `application/sylk-storage-changed` with `namespace == "avatars"`:
- Ignore it if `device_id` is our own.
- `op == "put"`: if a contact references `id` and the cached etag differs, GET it.
- `op == "delete"`: drop `id` from the cache and refresh the views showing it.
- The XCAP change (the new `avatar` attribute) arrives separately through the normal XCAP notification, so the two can arrive in either order. A contact pointing at an id not yet fetched simply fetches it.

### Upload (contact editor)
1. Crop and scale the picture, then encode it.
2. `PUT {storage_url}/avatars/<new uuid>` with `If-None-Match: *` and `X-Sylk-Device-Id`.
3. On 201: cache the bytes under the new id, set the contact's `avatar` attribute, and save the contact to XCAP.
4. DELETE the old id, if any (best effort).
5. Errors:
   - 413 or 507: tell the user (size or quota) and keep the old avatar.
   - 401: refresh the token once.
   - Network error: keep the picture locally and retry later. Do not set the XCAP attribute until the upload has succeeded.

### Accounts without SylkServer
Avatars stay local only, as before. No `avatar` attribute is written.

### UI
The contact list, contact editor and Alert panel (incoming call) show the same avatar, from the same cache.

## Open decisions
- **Encrypting avatars** with the account's own PGP key (Content-Type `application/pgp-encrypted`), so the server operator cannot see them.
  - Costs a decrypt per avatar, and the key must be present before avatars can be shown.
  - Not decided. Start unencrypted; the content type tells clients which form they got.
- **Own avatar**: stored the same way on the own contact, or also published via presence? Not decided.
- **Migration** of existing local avatars: upload them on the first run with storage available, only for contacts without an `avatar` attribute. Proposed, not decided.
- **Other namespaces** (preferences, drafts, group metadata): possible later.

## Implementation order
1. Blink Cocoa.
2. Blink Qt, ported from Cocoa so both behave the same.
3. Sylk Mobile and sylk-desktop.
