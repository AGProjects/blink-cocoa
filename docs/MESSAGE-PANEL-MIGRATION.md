# SIP Message UI — Migration to a Native Side Panel

Status: proposal / plan of record
Target: Blink for macOS (PyObjC), branch off current tree
Author: drafted from a full read of the current SMS/chat stack

---

## 1. Goal

Replace the tabbed **Instant Messages** window with a **conversation view inside the existing side
drawer**, and render the transcript with **native Cocoa views** instead of the WebKit `WebView` +
`ChatView.html`.

End state:

- One drawer, **two views inside it**: the existing audio stack, and the new messages view. One is
  visible at a time.
- No tab management. The **contact list is the conversation switcher** (Telegram Desktop model).
- Starting a message session opens the drawer showing that contact's conversation.
- While the messages view is showing, selecting another contact switches the conversation.
- **Unread counts live on the contact rows**, where the conversation switcher is.
- Background conversations stay fully alive: they keep sending, receiving, retrying, decrypting
  and writing history whether or not they are the one on screen.
- The old `SMSWindowController` window keeps working, unchanged, behind a flag, until it is
  phased out.

### Decisions taken

| Question | Decision |
|---|---|
| Panel host | **Reuse the existing audio-calls `NSDrawer`** |
| Drawer contents | **Two sibling views, swapped** — audio stack *or* messages view, never both |
| Transcript view | **`ListView` / `VerticalBoxView`** — the existing in-house vertical stack |
| Scope | **New native stack for the panel only**; old window, MSRP chat and History Viewer untouched |
| V1 features | Text + history scroll-back + delivery status, inline images, location bubbles, **per-contact unread badges** |

Swapping rather than stacking is what makes this cheap: the messages view always gets the drawer's
full height, the two views never compete for space, and `recalculateDrawerSplitter()` — the manual
frame math that lays out audio/participants/video — is untouched, because it only ever runs while
the audio view is showing. The one thing swapping costs is that an active call is not visible
while you are reading a conversation; see §6.4 and §8 for how to handle that.

---

## 2. The key insight that makes this cheap

`SMSViewController` — 93 KB of send/receive queues, OTR, PGP, IMDN, DNS routing, history replay
and location metadata — **never imports the renderer**. It reaches it exclusively through the
`chatViewController` IBOutlet, which is wired inside `Base.lproj/SMSView.xib`
(`SMSViewController.py:135`, nib loaded at `:246`).

> **Swap the nib, and you swap the renderer.** No change to the messaging logic.

Symmetrically, `SMSViewController`'s coupling to its *window* is six calls, not a hundred:

- `window.isKeyWindow()` — `SMSViewController.py:601` (gate for the native notification)
- `window.noteView_isComposing_(self, flag)` — `:869`, `:878`
- `self.windowController` — stored at `:166`, set at `SMSWindowManager.py:1549`, rarely read
- and, from the manager side, `windowForViewer(viewer).noteNewMessageForSession_` /
  `noteNoMessageForSession_` / `window()` — `SMSWindowManager.py:1432, 1494, 1918, 1936, 1941, 1943`

So the migration is: **define two duck-typed protocols, implement each once natively, and let
`SMSWindowManagerClass` choose the host at conversation-creation time.** No fork of the messaging
logic, no duplicated crypto.

---

## 3. Current architecture (what we are moving away from)

```
ContactWindowController.sendMessageToURI()            :1274
        │
        ▼
SMSWindowManagerClass.getWindow(target, …)            SMSWindowManager.py:1515
        │  ├─ linear scan windows × viewers → matchesTargetOrInstanceAndAccount()
        │  ├─ creates SMSViewController if none                       :1554
        │  └─ creates/reuses SMSWindowController, addViewer()         :1543-1576
        ▼
SMSWindowController  (NSWindowController, SMSSession.xib)   :106
        ├─ NSTabView (noTabsNoBorder) + FancyTabSwitcher
        ├─ unreadMessageCounts{} → tab badge                          :194-238
        ├─ NSToolbar: audio / video / encryption / smileys / print / history  :362
        └─ 10 s heartbeat timer over its own viewers                  :128-134
        ▼
SMSViewController  (SMSView.xib)                       SMSViewController.py:129
        ├─ outgoing_queue / incoming_queue / not_read_queue           :192-194
        ├─ OTR (OTRTransport), PGP, IMDN, DNS routing, history replay
        └─ chatViewController  ──IBOutlet──▶ ChatViewController
        ▼
ChatViewController  (ChatViewController.py:194)
        └─ outputView: ChatWebView (legacy WebKit1 WebView)
             └─ ChatView.html  ── JS: renderMessage(), markDelivered(), … ──
```

Everything on screen is a string of HTML spliced into `chat_session.innerHTML` via
`stringByEvaluatingJavaScriptFromString_` (`ChatViewController.py:789`).

### The drawer we are moving into

`NSDrawer` id **475**, `MainWindow.xib:1364` — trailing edge, `contentSize 267×100`,
`minContentSize 267×0`, `maxContentSize 10000×10000`, parent window 371, delegate
`ContactWindowController` (389).

```
drawer 475
└── contentView 474  "Audio Sessions Drawer View"
    ├── splitView 1524  "Drawer Main View"  (fixedFrame, delegate 389)   ┐
    │   ├── scrollView 544   → ListView 547            audioSessionsListView
    │   ├── scrollView 1534  → ParticipantsTableView 1537                 ├─ the "audio view"
    │   └── BlackView C73…   → VideoWidget YUX-UC-DK5  videoView          │
    └── customView FxS-ae-1a2  (30 pt bar: Hangup All tag 10, Conference tag 11, Mute, Silent) ┘
```

Audio layout is entirely manual frame math: `recalculateDrawerSplitter()`
(`ContactWindowController.py:2293`) computes h1/h2/h3, stores them in `drawerSplitterPosition`, and
`resizeDrawerSplitter()` (`:2273`) applies them with `setFrame_`. Panes with nothing to show get
height 0. If video would overflow, it is pushed to a standalone window.

Open/close: `showAudioDrawer()` `:2890` (only `if not isOpen and self.has_audio`),
`toggleAudioSessionsDrawer_` `:4117` (Window ▸ Audio Calls ⌘2), `drawerDidOpen_` `:4089`,
`drawerDidClose_` `:4109`, auto-close when the audio list empties `:3011-3013`.

**The messages view becomes a third sibling of contentView 474**, occupying its full bounds, and
the swap is `setHidden_` on the audio siblings vs. the messages view. See §6.4.

---

## 4. Target architecture

### 4.1 New files

| File | Role |
|---|---|
| `MessageHost.py` | Documents the **host protocol**; small shared helpers |
| `MessagePaneController.py` | The drawer's messages view — implements the host protocol |
| `NativeChatViewController.py` | Renderer — implements the **renderer protocol** natively |
| `MessageListView.py` | `ListView` subclass tuned for a transcript |
| `MessageBubbleView.py` | Per-message `NSView`: text, image, location, system |
| `MessageMapView.py` | Native location bubble (MapKit snapshot) |
| `Base.lproj/MessagePane.xib` | Messages view: header bar + conversation container + empty state |
| `Base.lproj/MessageView.xib` | Copy of `SMSView.xib` with the native renderer wired in |

All must be added to the **Resources** build phase of every target in
`Blink.xcodeproj/project.pbxproj` (`.py` files are bundled as resources here — see the existing
`SMSViewController.py in Resources` entries at `project.pbxproj:168, 381, 882`). Xibs go into
`Base.lproj` first; other `*.lproj` copies come later via `merge_xib_translations.sh`.

### 4.2 Renderer protocol

`NativeChatViewController` must be a drop-in duck-type of `ChatViewController` for everything
`SMSViewController` touches:

**Content**
`setContentFile_` (no-op), `setAccount_`, `resetRenderedMessages`, `clear`,
`showMessage(call_id, msgid, direction, sender, icon_path, content, timestamp, is_html, state, recipient, is_private, history_entry, media_type, encryption, before)`,
`showSystemMessage(content, timestamp, is_error, call_id)`,
`showLocationMessage(...)`, `updateLocationMessage(...)`, `setLocationMessageStatus(msgid, text)`,
`updateMessage(msgid, content, is_html, expandSmileys)`, `toggleSmileys(expand)`

**State / chrome**
`markMessage(msgid, state, private)`, `updateEncryptionLock(msgid, encryption)`,
`markFound(msgid)` / `unmarkFound(msgid)`, `htmlBoxVisible/Hidden(msgid)`,
`scrollToBottom()`, `scrollToId(id)`, `setHandleScrolling_(bool)`,
`appendAttributedString_(s)`, `resetTyping()`, `close()`

**Outlets** (wired in `MessageView.xib`, same names as `SMSView.xib`)
`view`, `outputView`, `inputText`, `lastMessagesLabel`, `loadingProgressIndicator`,
`loadingTextIndicator`, `searchMessagesBox`, `showRelatedMessagesButton`, `delegate`

**Properties**
`expandSmileys`, `search_text`, `scrolling_zoom_factor`, `last_sender`, `previous_msgid`,
`rendered_messages`, `handle_scrolling`, `textWasPasted`, `finishedLoading` (always `True`)

**IBActions** `searchMessages_`, `showRelatedMessages_`

**Delegate callbacks it must still fire** (`SMSViewController` implements all of these today):
`chatViewDidLoad_`, `chatView_becameActive_`, `chatView_becameIdle_`, `delete_message`,
`scroll_back_in_time`, `zoom_period_label`, `isOutputFrameVisible`.

`ChatInputTextView` (`ChatViewController.py:90`) is reused **verbatim** — zero WebKit dependency,
already reused standalone by `ChatPrivateMessageController.py:19`. `processHTMLText`, `AutoLink`
and `MSG_STATE_*` are reused as-is too.

### 4.3 Host protocol

Both `SMSWindowController` (today) and `MessagePaneController` (new) implement:

```
window()                              -> NSWindow      # main window for the pane
addViewer(viewer, focusTab=False)
removeViewer_(viewer)
viewers                                                # iterable of SMSViewController
selectedSessionController()           -> viewer|None
noteNewMessageForSession_(viewer)
noteNoMessageForSession_(viewer)
noteView_isComposing_(viewer, flag)
updateEncryptionWidgets(viewer=None)
```

`SMSWindowController` already implements all eight (`SMSWindowManager.py:139, 194, 221, 240, 250,
263, 270, 468`). No change needed there beyond documenting the contract.

### 4.4 Manager change

`getWindow()` (`SMSWindowManager.py:1515`) conflates three jobs. Split it:

```python
def viewerForTarget(target, display_name, account, instance_id=None,
                    create_if_needed=True, selected_contact=None):
    """find-or-create the SMSViewController. No UI. No window."""

def presentViewer(viewer, focus=True, note_new_message=True):
    """hand the viewer to the active host and bring it to the front."""

def getWindow(...):                       # thin back-compat wrapper, unchanged signature
    v = viewerForTarget(...)
    ...IMDN handling as today (:1533-1551)...
    presentViewer(v, focus=focusTab, note_new_message=note_new_message)
    return v
```

`presentViewer` is the single branch point on the feature flag: flag off → `SMSWindowController`
as today; flag on → `MessagePaneController`.

The existing three call sites in `ContactWindowController.py` (`:1302`, `:3586`, `:4581`) and the
`raiseLastWindowFront()` call at `:3560` do not change at all in phases 0–2.

Also replace the O(n·m) `windowForViewer()` scan (`:1589`) with a `viewer → host` dict maintained
by `addViewer`/`removeViewer_`.

---

## 5. Migration phases

Each phase is one recordable darcs patch, independently shippable, with the flag off.

### Phase 0 — Seams, no behaviour change

| # | Task | Files |
|---|---|---|
| 0.1 | Document the host protocol; assert `SMSWindowController` conforms | `MessageHost.py` (new) |
| 0.2 | Parametrize the nib: `nib_name = "SMSView"` class attribute, used at the `loadNibNamed_owner_` call | `SMSViewController.py:246` |
| 0.3 | Move the 10 s heartbeat from the **window** to the **manager**, iterating every live viewer | `SMSWindowManager.py:128-134, 516+` |
| 0.4 | Split `getWindow` into `viewerForTarget` + `presentViewer` + wrapper | `SMSWindowManager.py:1515-1576` |
| 0.5 | `viewer → host` dict; rewrite `windowForViewer` | `SMSWindowManager.py:1589` |
| 0.6 | Migration flags as module constants in `MessageHost.py` (`USE_MESSAGE_PANEL`, `USE_NATIVE_MESSAGE_RENDERER`), read in `presentViewer` / `nibName`; logged once at startup | `MessageHost.py`, `SMSWindowManager.py` |
| 0.7 | Rename `windowController` → `host` on the viewer, keep `windowController` as an alias property | `SMSViewController.py:166` |

**Why 0.3 matters:** the heartbeat currently belongs to `SMSWindowController` and drives retry of
`MSG_STATE_FAILED_LOCAL` messages plus DNS re-lookup (`SMSViewController.py:299`). A viewer not
hosted by a window would silently stop retrying. Moving it to the manager also fixes the existing
timer-invalidation bug in `close_` (`SMSWindowManager.py:273`), where the timer is only invalidated
inside the `unreadMessageCounts` branch.

**Acceptance:** behaviour identical. Regression pass on: open a conversation, send, receive,
delivery ticks, OTR start/stop, PGP, tab badges, drag a tab out.

---

### Phase 1 — Native renderer, tested inside the *old* window

The trick that de-risks everything: build the renderer first and prove it against the existing,
known-good window before any drawer work starts.

| # | Task |
|---|---|
| 1.1 | `MessageListView(ListView)` — see §6.1 |
| 1.2 | `MessageBubbleView` for text + system messages — see §6.2 |
| 1.3 | `NativeChatViewController` implementing the renderer protocol (text only; location/image stubbed to a placeholder bubble) |
| 1.4 | `MessageView.xib`: copy `SMSView.xib`, replace the `ChatWebView` with `NSScrollView` → `MessageListView`, retarget the `chatViewController` object's `customClass` to `NativeChatViewController` |
| 1.5 | Port the scroll-back state machine from `ChatViewController.isScrolling_` (`:845-897`) onto `NSViewBoundsDidChangeNotification` — see §6.3 |
| 1.6 | `USE_NATIVE_MESSAGE_RENDERER` makes `SMSViewController.nibName()` return `"MessageView"` |

**Acceptance:** with `USE_NATIVE_MESSAGE_RENDERER` on and `USE_MESSAGE_PANEL` off, the *existing tabbed
window* renders every conversation natively. Send, receive, ticks, encryption lock, deferred glyph,
delete, sender grouping, smileys, links, search highlight, infinite scroll-back all work.
Everything else about the app is untouched.

---

### Phase 2 — The messages view inside the drawer

| # | Task |
|---|---|
| 2.1 | Add a third sibling `customView` to drawer contentView 474 in `MainWindow.xib`, full bounds, autoresizing on all edges; outlet `messagesDrawerView` on `ContactWindowController` |
| 2.2 | `MessagePane.xib`: header bar (avatar, name, presence/URI, encryption lock popup, audio/video/history/print/smileys buttons, typing line) + conversation container + empty-state label |
| 2.3 | `MessagePaneController` implementing the host protocol; owns the messages view, the header, and a `viewer → contentView` map |
| 2.4 | `showDrawerView_('audio' \| 'messages')` on `ContactWindowController` — the swap — see §6.4 |
| 2.5 | Drawer open/close and ⌘2 / ⌘4 semantics — see §6.4 |
| 2.6 | Per-view drawer width, remembered separately — see §6.4 |
| 2.7 | Port the encryption menu: `menuWillOpen_` (`SMSWindowManager.py:385`) and `updateEncryptionWidgets` (`:468`) — pure chrome, reads state off the viewer |
| 2.8 | Port `toolbarButtonClicked_` (`:362`) actions to the header buttons: audio/video → `contactWindow.startSessionWithTarget`, smileys toggle, history viewer |
| 2.9 | `presentViewer` honours `USE_MESSAGE_PANEL`; Window ▸ Messages ⌘4 (`showSMSWindow_`, `ContactWindowController.py:3965`) shows the messages view instead of opening the window |

`addViewer` = `container.addSubview_(viewer.getContentView())` + frame to bounds, shown only if it
is the selected conversation. `getContentView()` (`SMSViewController.py:1552`) already returns a
detached `NSView`, so reparenting is free.

**Acceptance:** with both flags on, ⌘4 and "Send Short Message…" open the drawer on the messages
view with the right conversation. ⌘2 swaps to audio and back without disturbing the transcript,
scroll position or unsent input. With flags off, the old window is exactly as before.

---

### Phase 3 — Contact list drives the conversation

| # | Task |
|---|---|
| 3.1 | In `contactSelectionChanged_` (`ContactWindowController.py:4020`): **if and only if** the messages view is showing, switch it to the selected contact's conversation (created lazily via `viewerForTarget`) |
| 3.2 | Conversation switch = hide current content view, show/insert the new one, update the header, `scrollToBottom` if it was at the bottom |
| 3.3 | Empty state when the selected row has no messageable URI, or a group row is selected |
| 3.4 | Add a selection observer for `searchOutline` — it has none today (only `contactOutline` is observed, `:508`) |
| 3.5 | Tab-specific paths become unreachable in pane mode (`dettachSMSViewer`, `tabView_didDettachTabViewItem_atPosition_`); optionally add an "Open in separate window" header action that hands the viewer back to `SMSWindowController` |

**Design rule (important):** selection only *switches* the conversation; it never *opens* the
drawer or swaps to the messages view. Otherwise selecting a contact to place an audio call would
yank the drawer away from the call list. The messages view is shown explicitly: ⌘4, the Messages
menu action, double-click on a contact whose `preferred_media == 'messages'`
(`ContactWindowController.py:4669`), or an incoming message.

**Acceptance:** with the messages view showing, arrowing through the contact list walks
conversations, each keeping its own scroll position and unsent input text.

---

### Phase 4 — Focus, read receipts and unread badges

The tabbed window derives "the user is looking at this conversation" from *window is key* + *tab is
selected* (`windowDidBecomeKey_` `:336`, `windowDidResignKey_` `:331`,
`tabView_didSelectTabViewItem_` `:289`). That drives `not_read_queue_start/stop()` and therefore
outgoing IMDN **display** notifications. Getting it wrong means telling the far end you read
messages you never saw.

| # | Task |
|---|---|
| 4.1 | `isConversationVisible(viewer)` = main window is key **AND** drawer is open **AND** the drawer is showing the **messages** view **AND** viewer is the selected conversation |
| 4.2 | Fire `not_read_queue_start/stop` + `send_read_messages_notifications()` on every transition: main window key/resign, `drawerDidOpen_`/`drawerDidClose_`, **view swap**, conversation switch |
| 4.3 | Unread counts on the manager, keyed by conversation; `BlinkUnreadMessageCountChanged` notification — see §6.5 |
| 4.4 | `ContactCell.setUnreadCount_` + badge drawing; wire through `outlineView_willDisplayCell_forTableColumn_item_` for **both** outlines — see §6.5 |
| 4.5 | `chat_messages.read` column, schema version 8 → 9, so unread survives relaunch — see §6.5 |
| 4.6 | `noteView_isComposing_` → a "typing…" line under the header (replacing the tab dot, `FancyTabSwitcher.setComposing_`) |

The view swap is now a first-class read-receipt boundary: swapping to audio must stop the read
queue exactly as resigning key does. This is the single easiest thing to get wrong in Phase 4.

**Acceptance:** no IMDN `displayed` is emitted while the drawer shows the audio view; swapping to
messages on an unread conversation emits it once and clears the badge; the badge is still correct
after quitting and relaunching.

---

### Phase 5 — Inline images

`SMSViewController` does not produce `is_html=True` image payloads today (only the MSRP
`ChatController` does, `:1605` and `:1738`), so this is forward-looking.

- Image bubble: content sniffed as `data:` URI or local file path → `NSImage`, aspect-scaled to
  `min(600, bubbleWidth)`, click opens in Preview / Quick Look.
- Conservative HTML fallback for anything else arriving with `is_html=True`: strip tags to plain
  text rather than attempting to render markup. Full HTML is deliberately out of scope — that is
  precisely what the old window keeps the WebView for.

---

### Phase 6 — Location bubbles

Today these are pure JS+CSS in `ChatView.html:558-780`: a hand-computed OSM/Mapnik slippy-map tile
grid, pin, destination pin, `±N m` accuracy label, and a lifecycle footer. Reproducing the tile
math natively also means writing an HTTP tile fetcher, a disk cache and @2x handling.

**Recommendation: use `MKMapSnapshotter` instead.** Native, asynchronous, retina-correct, no tile
math, no cache to maintain, and Apple Maps matches the `maps.apple.com` link the bubble already
opens (`ChatViewController.py:571-664`).

- `MessageMapView` requests a snapshot for (lat, lng, accuracy → span), draws the 📍 pin and the
  optional green destination pin over it, renders the coords + accuracy line and the status footer.
- `updateLocationMessage` (live tracking) re-requests the snapshot, coalesced to at most one
  request per few seconds per bubble.
- `setLocationMessageStatus` updates the footer text only.
- Click → `NSWorkspace.openURL_` with the same `maps.apple.com` URL built today.

If Apple Maps is unacceptable for policy reasons, the fallback is a straight port of
`locLatLngToTileFrac` plus an `NSURLSession` tile cache — budget roughly 3× the MapKit path.

---

### Phase 7 — Parity chrome

| Item | Today | Native view |
|---|---|---|
| Print transcript | `outputView.mainFrame().frameView().documentView().print_()` (`SMSWindowManager.py:489`) | `NSPrintOperation.printOperationWithView_(messageListView)`; give the list view `knowsPageRange_`/`rectForPage_` so bubbles do not split across pages |
| Text zoom | `outputView.makeTextLarger_/Smaller_` | Font-size setting on the renderer → `relayoutAll()` |
| Select & copy across messages | free from WebView | Per-bubble selection is free (selectable `NSTextField`); implement `copy_` on `MessageListView` to concatenate all/selected bubbles. **A genuine regression to design for.** |
| Transcript search | re-queries history and re-renders; `markFound` colours the body red | Same path; `markFound` sets the bubble's body text colour |
| Links | `urlify()` → `<a>` | `NSDataDetector` + `NSLinkAttributeName` on the selectable text field — no urlify needed |
| Smileys | `<img>` substitution via `SmileyManager` | `NSTextAttachment` in the attributed body; `toggleSmileys` re-renders bodies |
| Add-contact banner | detached `addContactView`, inserted programmatically (`SMSViewController.py:416`) | Same view, inserted into the messages header area |
| File drag & drop | registered on `ChatWebView`, forwarded to `delegate.sendFiles` | `SMSViewController` has **no** `sendFiles` — inert today. Register on `MessageListView` only when/if the SMS path gains file transfer |
| Collaboration editor | MobWrite embedded in `ChatView.html:993` | **Not ported.** MSRP-chat-only feature; stays with the WebView |

---

### Phase 8 — Phase out the old window

Only after the messages view has shipped and settled:

1. Flip `USE_MESSAGE_PANEL` to `True`; keep the escape hatch for one release.
2. Remove the flag; `presentViewer` always uses the drawer.
3. Delete `SMSWindowController` (`SMSWindowManager.py:106-514`), `Base.lproj/SMSSession.xib` and
   its `*.lproj` copies, and the `SMSView.xib` + `ChatViewController` path for SMS.
4. `FancyTabSwitcher` stays — `ChatWindowController` (MSRP chat) still uses it.
5. `ChatViewController` + `ChatView.html` stay — still used by `ChatController` and
   `HistoryViewer`. Converting those is a separate project.

---

## 6. Detailed designs for the parts that will bite

### 6.1 `MessageListView(ListView)`

`ListView`/`VerticalBoxView` were built for a handful of fixed-height audio rows. A transcript
breaks four of their assumptions. All four are fixable inside a subclass:

1. **`minimumHeight()` is O(n) and is called on every insert** (`ListView.py:82, 92`), so appending
   N messages is O(n²). → Maintain a running `_total_height`; override `minimumHeight`.
2. **`relayout()` never recomputes item heights** — `resizeWithOldSuperviewSize_`
   (`VerticalBoxView.py:52`) sets each subview's *width* to the scroll view's content width but
   leaves the height alone. Bubbles are height-for-width. → Override `relayout` to call
   `view.layoutForWidth_(w)` on each item whose width changed, then stack.
3. **Alternating rows and selection highlight** (`ListView.py:45-62`) are wrong for a transcript.
   → `alternateRows = False`, `allowSelection = False`, `textBackgroundColor` background.
4. **`mouseDown_` uses `convertPointFromBacking_`** (`ListView.py:92, 130`), a coordinate-space bug
   (backing ≠ window space on retina). → Not inherited: bubbles handle their own mouse events. Do
   **not** fix it in `ListView` itself — the audio list depends on current behaviour.

Added API:

```python
beginUpdates() / endUpdates()      # suppress relayout during batch history replay
appendMessageView_(view)           # → insertItemView_before_(view, None)
prependMessageView_(view)          # → insertItemView_before_(view, self.subviews()[0])
viewForMessageId_(msgid)           # dict lookup; replaces document.getElementById
removeMessageId_(msgid)
scrollToBottom() / scrollToMessageId_(msgid)
relayoutAll()
```

**Scroll anchoring on prepend** (history scroll-back inserts *above* the viewport): record
`documentVisibleRect().origin.y` and `_total_height` before the batch, then after `endUpdates`
scroll to `old_origin_y + (new_total - old_total)`. Without this, every scroll-back jumps.

**Memory**: `ListView` has no view recycling — every message view stays alive. With
`showHistoryEntries = 25` per page (`SMSViewController.py:143`) that is fine, but scrolling back to
"all messages" (zoom factor 7) can materialise thousands of views. Add a hard cap (e.g. 2000) that
trims from the top with a "load newer" affordance. This is the main structural cost of choosing
`ListView` over a view-based `NSTableView`.

**Note on the swap:** hiding the messages view does not deallocate it, so transcripts survive
swapping to audio and back with zero rebuild — no scroll restore needed, no history re-query. That
is a real benefit of `setHidden_` over tearing the view down.

### 6.2 `MessageBubbleView(NSView)`

One class configured by kind (`text`, `image`, `location`, `system`) rather than four classes — it
keeps the header chrome in one place.

Subviews: avatar `NSImageView` (32 px, hidden when grouped) · sender `NSTextField` (hidden when
grouped) · header row: delete button, encryption lock `NSImageView`, timestamp, deferred 🕿,
delivered ✔, displayed ✔✔ · body (`NSTextField` selectable / `NSImageView` / `MessageMapView`).

`layoutForWidth_(w)` computes height with
`NSAttributedString.boundingRectWithSize_options_(NSMakeSize(bodyWidth, 0), NSStringDrawingUsesLineFragmentOrigin)`
and caches per `(width, contentHash)`.

`drawRect_` paints the bubble with `NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(…, 5, 5)`,
1 px `#A9A9A9` border, mapping `MSG_STATE_*` (`ChatViewController.py:30-36`) to existing colours:

| State | Fill |
|---|---|
| default / sent / delivered / displayed / deferred | `textBackgroundColor` (white) |
| sending | `#EEEEEE` |
| failed / failed_local | `#EFD4DA` |
| private | `#CCFFCC` |

Direction: outgoing right-aligned at 90 % width, incoming left-aligned at 90 % — matching
`.local_container` / `.remote_container` (`ChatView.html:48-158`). Sender name colours: remote
`#436294`, self `#629443`.

**Sender grouping** must be reproduced: today Python passes `icon_path = "null"` when
`sender == self.last_sender` (`ChatViewController.py:520-524`) and the JS hides the avatar and
sender row and softens the *previous* bubble's bottom border. Natively: keep `last_sender` /
`previous_msgid` on the renderer, and on a grouped message call `setGroupedWithPrevious_(True)` on
the new bubble and `setContinuedBelow_(True)` on the previous one.

**Message index**: `self.message_views = {msgid: view}`, replacing the DOM id convention
(`b<msgid>`, `r<msgid>`, `encryption<msgid>`, `delivered<msgid>`, …). Note that
`htmlBoxVisible/Hidden` are called with `'c%s' % msgid` (`ChatViewController.py:346`), an id that
does not exist in the DOM — those two are dead today. Implement them correctly natively and
search-hiding starts working for free.

### 6.3 Scroll-back without a WebView

The current state machine hangs off `<body onscroll> → blink.isScrolling_(scrollTop)`
(`ChatViewController.py:846-897`): `scrollTop < 0` (rubber-band overscroll at the top) starts a 1 s
timer; if still overscrolled when it fires, bump `scrolling_zoom_factor`, set the "Loading messages
from last …" label and call `delegate.scroll_back_in_time()`.

Native equivalent, same logic verbatim:

```python
scrollView.contentView().setPostsBoundsChangedNotifications_(True)
NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
    self, "boundsDidChange:", NSViewBoundsDidChangeNotification, scrollView.contentView())
```

`scrollTop = contentView.bounds().origin.y`, which goes **negative** during elastic overscroll
exactly like the WebView's `document.body.scrollTop` — so `isScrolling_` ports unchanged. Requires
`scrollView.setVerticalScrollElasticity_(NSScrollElasticityAllowed)` (the default).

### 6.4 One drawer, two views — the swap

**Structure.** Do **not** reparent the existing views. Add the messages view as a **third sibling**
of drawer contentView 474, sized to its full bounds with all-edge autoresizing. The swap is then
three `setHidden_` calls and nothing else:

```python
def showDrawerView_(self, which):          # 'audio' | 'messages'
    audio = (which == 'audio')
    self.drawerSplitView.setHidden_(not audio)          # splitView 1524
    self.drawerBottomBar.setHidden_(not audio)          # customView FxS-ae-1a2
    self.messagesDrawerView.setHidden_(audio)
    self.currentDrawerView = which
    self.applyDrawerWidthForCurrentView()
    if audio:
        self.recalculateDrawerSplitter()
    self.updateConversationVisibility()                 # §Phase 4.1
```

This keeps `splitView` 1524, its three panes, the bottom bar and every existing outlet exactly
where they are. `recalculateDrawerSplitter()` (`:2293`) needs one added guard — return early when
`currentDrawerView != 'audio'` — alongside its existing `if not self.drawer.isOpen(): return`
(`:2295`). Nothing else in the audio layout code changes.

**The bottom bar comes along for free.** Hangup All / Conference / Mute / Silent (`FxS-ae-1a2`) is
audio-specific chrome and is a sibling, so hiding it with the split view is exactly right — a
messages-only drawer has no orphaned call buttons, with no extra work.

**Width, remembered per view.** The drawer is 267 pt wide (`contentSize` and `minContentSize` both
267, `MainWindow.xib:1365-1366`). A transcript at 267 pt is unusable; audio rows are designed for
267 and stretch badly when wider (`ListView.relayout` stretches item views to the scroll view's
width, `VerticalBoxView.py:52`, and `AudioSession.xib` is springs-and-struts).

Keep two widths and apply on swap:

```python
DRAWER_WIDTHS = {'audio': 267, 'messages': 480}     # seeded, then user-adjustable

def applyDrawerWidthForCurrentView(self):
    w = defaults.get('DrawerWidth_' + self.currentDrawerView, DRAWER_WIDTHS[...])
    self.drawer.setMinContentSize_(NSMakeSize(267 if audio else 360, 0))
    self.drawer.setContentSize_(NSMakeSize(w, self.drawer.contentSize().height))
```

Record the width back into defaults on `drawerDidResize`. Because audio always returns to its own
remembered width, the audio-row stretching concern largely disappears — it only appears if the user
deliberately widens the drawer while on audio, which is their choice.

**Height.** A drawer's height is bounded by its parent window's; main window `minSize` is 274×132
(`MainWindow.xib:514`). On first show of the messages view, if the window is shorter than ~600 pt,
grow it and remember that we did. Swapping needs no height logic — the messages view simply fills
the drawer.

**Open / close.** Today the drawer opens only when there is audio (`showAudioDrawer`, `:2890`) and
auto-closes when the audio list empties (`:3011-3013`). Both widen:

- `showAudioDrawer()` keeps its name and behaviour, plus `showDrawerView_('audio')`.
- Add `showMessagesDrawer()`: open the drawer if closed, then `showDrawerView_('messages')`.
- `finalizeAudioSession` (`:3004`) closes the drawer only if `currentDrawerView == 'audio'` and no
  audio sessions remain. If the user is on messages, the drawer stays open and untouched.
- **⌘2** (Window ▸ Audio Calls, `toggleAudioSessionsDrawer_` `:4117`): closed → open on audio;
  open on messages → swap to audio; open on audio → close.
- **⌘4** (Window ▸ Messages, `showSMSWindow_` `:3965`): mirror image.
- Closing the drawer never tears down conversations — it only hides them.

**The one real cost of swapping: a call is invisible while you read.** The user chose not to
auto-swap to audio on an incoming call, so `showAudioDrawer()` — called from `:2887`, `:6143`,
`:6145`, `:6156` — must **not** steal the view when `currentDrawerView == 'messages'`. It should
open the drawer if closed, but leave the current view alone. Mitigations, in order of cost:

1. `AlertPanel` already handles incoming-call alerting independently of the drawer
   (`AlertPanel.py:66`), so a ringing call is not actually hidden.
2. Put a small live badge on the messages header (or the ⌘2 menu item) showing the number of active
   audio sessions, so the user can see there is a call and one click away.
3. If this proves annoying in practice, revisit the "auto-show audio during a call" variant — the
   swap machinery supports it with a two-line change plus a "return to messages when the call ends"
   memory.

Ship (1) + (2); leave (3) as a documented escape hatch.

**Why this shape.** One drawer means one place layout is decided, no `preferredEdge` conflict, no
second `NSDrawer` instance, and audio stays exactly where users expect it. Swapping means the
messages view always gets the full drawer height and `recalculateDrawerSplitter` never has to
arbitrate between a video call and a transcript. `NSDrawer` is deprecated since 10.13, but we are
not adding a new dependency — we are reusing the one that already exists. Keep all messages content
inside `MessagePane.xib` owned by `MessagePaneController` so that swapping the whole drawer for an
`NSSplitView` later touches `ContactWindowController` and two methods, not the message code.

### 6.5 Per-contact unread badges

**Where counts live.** On `SMSWindowManagerClass`, not on a host — a conversation must accumulate
unread whether it is hosted by the drawer, the old window, or nothing at all:

```python
self.unread_counts = {}      # conversation_key -> int

def conversation_key(self, viewer_or_uri, account=None):
    # Bonjour conversations key on instance_id, everything else on canonical URI
    return instance_id if bonjour else self._canonical_uri(uri)
```

`_canonical_uri` already exists (`SMSWindowManager.py:1048`) and strips scheme, `;parameters`,
`?headers` and case. Bonjour keys on `BonjourBlinkContact.id`, matching how
`matchesTargetOrInstanceAndAccount` (`SMSViewController.py:441`) and `add_to_history`
(`SMSViewController.py:970-972`) already treat it.

- **Increment** where the manager already notifies the host (`SMSWindowManager.py:1936`), so the
  count is host-independent.
- **Clear** when the §Phase 4.1 visibility predicate becomes true for that conversation, and on
  remote `application/sylk-conversation-read` (`:1918`, which already calls
  `noteNoMessageForSession_`).
- **Announce** with a `BlinkUnreadMessageCountChanged` notification carrying `(key, count, total)`.
  Consumers: the contact list, the messages header, and `NSApp.delegate().noteNewMessage()` for the
  Dock badge (`BlinkAppDelegate.py:304`).

**Rendering in the contact list.** `ContactCell` is a cell-based `NSTextFieldCell`
(`ContactCell.py:29`), one shared instance per outline (`dataCell` id 508 for `contactOutline`,
id 626 for `searchOutline`), reconfigured per row in
`outlineView_willDisplayCell_forTableColumn_item_` (`ContactListModel.py:2325`). So:

```python
# ContactListModel.py:2326  — replace the dead line
cell.setMessageIcon_(None)
# with
cell.setUnreadCount_(self.unread_count_for_contact(item))
```

`unread_count_for_contact(contact)` sums `unread_counts` over all of `contact.uris` canonicalised
(a contact can have several URIs — the same fan-out `replay_history` already does when loading a
conversation), or over the Bonjour id.

`setMessageIcon_` (`ContactCell.py:57`) is dead scaffolding: called exactly once, always with
`None`, and `self.messageIcon` is never drawn. Replace it rather than adding a parallel path.

**Drawing — four traps in `drawWithFrame_inView_` (`ContactCell.py:68`):**

1. **`self.frame` is mutated in place and progressively consumed.** `drawFirstLine` does
   `frame.origin.x = 35; frame.origin.y += 6` (`:96-98`), `drawSecondLine` does
   `frame.origin.y += 16` (`:106`). Compute the badge rect from the **original** frame, before
   calling either — or draw the badge first.
2. **The whole body is wrapped in a bare `try / except Exception: pass`** (`:74-84`). A badge
   drawing bug will silently blank the entire row — no avatar, no name, no clue. Give the badge its
   own try/except that logs via `BlinkLogger`.
3. **Text must shrink to make room.** `drawFirstLine` draws into `frame.size.width - 10`,
   `drawSecondLine` into `frame.size.width - 25`. Subtract the badge width plus padding from both
   when a badge is present, so name and detail truncate (the paragraph style is already
   `NSLineBreakByTruncatingTail`) instead of running under the badge.
4. **Both outlines need it.** `searchOutline` has its own `ContactCell` dataCell (id 626) and its
   own rows; wiring only `contactOutline` gives half a feature.

Badge geometry, on the 44 pt contact row (`ContactListModel.py:2319`):

```
width  = max(18, textWidth + 10)          # "99+" cap
rect   = (frame.width - width - 8, frame.origin.y + 14, width, 16), corner radius 8
fill   = NSColor.systemBlueColor()        # or controlAccentColor
text   = white, bold, systemFontOfSize_(10), centred
highlighted row → invert: white pill, accent-coloured text
```

`drawActiveMedia` (`:115`) also draws from `frame.size.width - 8` leftwards, but returns
immediately for anything that is not a `BlinkConferenceContact` (`:117-118`), so ordinary rows
never collide. For conference contacts, start its icon run left of the badge.

**Refresh.** Observe `BlinkUnreadMessageCountChanged` in `ContactListModel`, map the key back to
contacts (`getContact` `:972` / `_findContactByCanonicalURI` `:999` already do this) and call the
existing `reloadModelItem(item)` (`ContactListModel.py:906` →
`contactOutline.reloadItem_reloadChildren_(item, True)`). Reload only the affected rows, never
`reloadData()` — the contact list is large and reloading it collapses scroll position.

**Persistence across relaunch — the part that needs a schema change.**

Unread cannot be derived from history as it stands:

- `ChatMessage` has **no** read/unread column (`HistoryManager.py:496-522`).
- Reading a message locally sends IMDN `displayed` (`_send_read_notification` →
  `sendIMDNNotification(id, 'displayed')`, `SMSViewController.py:867`) but does **not** update the
  local row's status.
- `messages_read()` (`:431`) only runs when a *remote* device reports the conversation read.
- `send_read_messages_notifications()` is a stub whose body is `return` (`:1207`).

So without a column, unread resets to zero on every launch — and worse, journal sync
(`syncIncomingMessage` `:1367`) persists messages received while the app was closed, which would
then never be counted at all.

Add the column. `HistoryManager._migrate_version` (`:557`) already contains the exact idiom:

```python
# ChatHistory.__version__: 8 -> 9
if next_upgrade_version < 9:
    query = "alter table chat_messages add column 'read' INTEGER DEFAULT 1"
    try:
        self.db.queryAll(query)
    except dberrors.OperationalError as e:
        if not str(e).startswith('duplicate column name'):
            BlinkLogger().log_error(...)
```

`DEFAULT 1` is deliberate: existing history migrates in as **read**, so nobody upgrades into a wall
of unread badges. Then:

- `add_message(...)` sets `read=0` for `direction='incoming'` only, and **`read=1` for journal
  messages older than the sync watermark** (`_persist_journal_message` `:1310`) — otherwise the
  first sync after upgrade lights up every contact.
- `_send_read_notification` and `messages_read` set `read=1` (a new
  `ChatHistory.mark_messages_read(remote_uri, local_uri)`).
- On launch, seed `unread_counts` with one query:
  `SELECT remote_uri, count(*) FROM chat_messages WHERE direction='incoming' AND read=0 GROUP BY remote_uri`.

~40 lines against an idiom already in the file, and it is what makes the badge trustworthy. It also
makes the stubbed `send_read_messages_notifications` implementable later.

---

## 7. File-by-file change table

| File | Change | Phase |
|---|---|---|
| `SMSWindowManager.py` | split `getWindow`; `viewer→host` dict; move heartbeat; feature flag; unread counts + notification | 0, 4 |
| `SMSViewController.py` | `nib_name` attribute; `windowController` → `host` alias. **Nothing else.** | 0 |
| `MessageHost.py` | new — host protocol | 0 |
| `MessageListView.py` | new | 1 |
| `MessageBubbleView.py` | new | 1 |
| `NativeChatViewController.py` | new | 1 |
| `Base.lproj/MessageView.xib` | new (from `SMSView.xib`) | 1 |
| `MessagePaneController.py` | new | 2 |
| `Base.lproj/MessagePane.xib` | new | 2 |
| `Base.lproj/MainWindow.xib` | third sibling `customView` in drawer contentView 474 + `messagesDrawerView` outlet + `drawerBottomBar` outlet | 2 |
| `ContactWindowController.py` | `showDrawerView_`, per-view width, drawer open/close semantics, `recalculateDrawerSplitter` guard, menu wiring, selection→conversation switch | 2–3 |
| `ContactCell.py` | `setUnreadCount_` replaces `setMessageIcon_`; badge drawing; text width reservation | 4 |
| `ContactListModel.py` | set the badge in `willDisplayCell`; `unread_count_for_contact`; observe `BlinkUnreadMessageCountChanged`; reload affected rows | 4 |
| `HistoryManager.py` | `chat_messages.read` column, version 8 → 9; `mark_messages_read`; unread seed query | 4 |
| `MessageMapView.py` | new | 6 |
| `Blink.xcodeproj/project.pbxproj` | add every new `.py`/`.xib` to Resources for **all** targets | each |
| `Base.lproj/AudioSession.xib`, `splitView` 1524 and its panes | **untouched** — the swap does not reparent or resize them | — |
| `ChatViewController.py`, `ChatView.html`, `ChatController.py`, `HistoryViewer.py`, `ChatWindowController.py`, `FileTransferSession.py` | **untouched** | — |

Three frozen copies of the tree must not be edited: `AppStore/…/Contents/Resources/`,
`build/Release/Blink.app/Contents/Resources/`,
`build_scripts/publish/dmg/staging/Blink.app/Contents/Resources/`. Also the `*.py~` backups.

---

## 8. Risks and open issues

| Risk | Mitigation |
|---|---|
| **An active call is invisible while the messages view shows** | `showAudioDrawer()` must not steal the view; `AlertPanel` still alerts; add an active-call badge on the messages header. Auto-swap remains a documented escape hatch (§6.4) |
| Read receipts sent while the drawer shows audio | The view swap is a first-class boundary in the Phase 4.1 predicate — the easiest thing to get wrong |
| `ListView` has no recycling; long transcripts materialise thousands of views | Hard cap + trim (§6.1); revisit as view-based `NSTableView` only if it bites |
| Unread badge bug silently blanks contact rows | The bare `except Exception: pass` at `ContactCell.py:74`; give badge drawing its own logged try/except (§6.5) |
| Journal sync lights up every contact after upgrade | `read` column defaults to 1; journal messages older than the watermark inserted as read (§6.5) |
| Audio rows stretch if the drawer is left wide | Per-view remembered widths (§6.4) — audio always returns to its own width |
| Cross-message copy is a regression vs WebView | Implement `copy_` on the list view (Phase 7); per-bubble selection is the primary path |
| Printing loses WebView pagination | `NSPrintOperation` on the list view; verify page breaks do not split bubbles |
| Location bubbles are the largest single unknown | `MKMapSnapshotter`; OSM tile port as fallback |
| A conversation ends up hosted by both window and drawer | One host per viewer, decided at creation in `presentViewer`; assert on `addViewer` |
| Bonjour conversations key on `instance_id`, not URI | `viewerForTarget` keeps `matchesTargetOrInstanceAndAccount` (`SMSViewController.py:441`) unchanged; contact-switching and unread keys use the same rule (§6.5) |
| `NSDrawer` deprecated since 10.13 | Not a new dependency — reusing the existing one. Contained behind `MessagePaneController` so a later `NSSplitView` port touches two methods |
| Localisation: 5 `*.lproj` trees | New xibs land in `Base.lproj` only; run `merge_xib_translations.sh` once strings settle |

### Deliberately out of scope

Collaboration editor (MobWrite), arbitrary HTML rendering, the MSRP chat window, History Viewer,
file transfer over SIP MESSAGE (`FileTransferSession.py` is MSRP-based and has no hook into the
message path today), and converting `ChatController`/`HistoryViewer` off the WebView.

---

## 9. Suggested sequencing

| Phase | Content | Rough size |
|---|---|---|
| 0 | Seams, flags, heartbeat move | small |
| 1 | Native renderer, proven inside the old window | **large — the core of the work** |
| 2 | Messages view in the drawer: swap, widths, open/close, header chrome | medium |
| 3 | Contact-list switching | small |
| 4 | Focus, read receipts, unread badges, `read` column | medium |
| 5 | Inline images | small |
| 6 | Location bubbles via MapKit | medium |
| 7 | Print, zoom, copy, search parity | medium |
| 8 | Flip default, then delete the old window | small |

Phase 1 is where the work is; everything else is incremental and independently shippable. Landing
it behind `USE_NATIVE_MESSAGE_RENDERER` means the renderer gets real mileage in the existing window
before the drawer work starts — if the native transcript is wrong, you find out with tabs still
there to fall back to.
