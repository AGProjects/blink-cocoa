# Blink for macOS — WebRTC video conferencing

How Blink for macOS joins SylkServer conference rooms with audio and video over WebRTC, next to
the MSRP chat it always had, and how it joins the rooms of other SylkServer domains. The Linux
client does the same (blink-qt `docs/webrtc/webrtc-conferencing.md`); this is the Cocoa side.

| | |
|---|---|
| Code | `WebRTCSupport.py` (availability, room addresses), `SylkRTC.py` (signalling), `Videoroom.py` (media, the room), `VideoroomWindowController.py` (the window), `ConferenceShareDialog.py` (share sheet); hooks in `ConferenceController.py`, `ContactWindowController.py`, `ChatController.py`, `ChatWindowController.py`, `SessionController.py`, `SessionRinger.py`, `AlertPanel.py`, `ContactCell.py`, `VideoWindowController.py` (`VideoCallBar`), `SIPManager.py` |
| Media | WebRTC.framework (libwebrtc M155, Objective-C API), installed by `build_scripts/05c_install_webrtc.sh`, copied into Blink and Blink Pro |
| Server side | SylkServer webrtcgateway (sylkRTC protocol, Janus videoroom), conference application (MSRP switch) |
| State | 2026-10-24: first version (join, grid, room chat over WebRTC, foreign domains, share sheet). 2026-10-25: the Qt changes of 2026-10-25 ported (§10): layouts, carousel, speakers, moderation, speaking, video loss, screen sharing, MSRP window and call details. Then: the room's controls by the number of people, a real button for our thumbnail, the Join panel's recent rooms, video loss by new frames (a still shared screen keeps its picture), a screencast source for our screen. Our own tile has no speaking border while we are alone in the room. **None of the 2026-10-25 work is tested yet**: only parsed and checked with pyflakes. §9 lists what to check first |

---

## 1. Overview

A SylkServer room is two things that share one name:

- the **video room**: a Janus videoroom (an SFU) behind the webrtcgateway, reached over a
  WebSocket with the sylkRTC protocol. Every participant publishes its camera and microphone on
  one PeerConnection and subscribes to each other publisher on one PeerConnection per publisher
  (a *feed*);
- the **MSRP switch**: the conference application, reached over SIP, carrying the room's chat
  (MSRP), its conference-info (participants) and, for SIP callers, the mixed audio. The gateway's
  audio bridge (`sylk-janus-audio-bridge`) joins the two.

Blink joins the video room for **audio and video** and the MSRP switch for **chat only**. Audio on
both would loop the room's audio, so joining the video room (Join panel with Video, a video call
to a room, the chat window's header Video) removes SIP audio and video from a session already
connected to the same room, or ends it when that was all it had (`WebRTCSupport.drop_sip_audio`).

```
            ┌──────────── WebSocket (sylkRTC-2) ───────────┐
Blink ──────┤  RTCPeerConnection: publisher + one per feed ├──► webrtcgateway ── Janus videoroom
            └──────────────────────────────────────────────┘              │ audio bridge
            ┌──────────── SIP + MSRP (chat) ────────────────┐              │
            └───────────────────────────────────────────────┴──► conference app (MSRP switch)
```

## 2. Loading and availability

Nothing of WebRTC.framework is loaded until an account's domain is found to publish a `wsServer`
in its SylkServer configuration (`SylkDiscovery`); then `WebRTCSupport.media_missing()` loads it
with `objc.loadBundle` (once a run) and checks the classes it needs.

| Check | Meaning |
|---|---|
| `ws_server(account)` | the `wsServer` of the account's domain (`_sylkserver.<domain>` TXT → configuration JSON) |
| `media_missing()` | what is missing: the framework (not in the app's Frameworks), a class, `NSURLSessionWebSocketTask` |
| `media_available()` | `media_missing()` empty |
| `conference_video_available(account)` | both |

The Activity log says what was found, once per change of default account, server or missing set
(`VideoConferenceAvailability`, started by `SIPManager`):

```
[webrtc] Video conferences available for ag@sylk.link through wss://…/webrtcgateway/ws
[webrtc] WebRTC media: WebRTC.framework 155.0.0 (M155), video VP8, VP9, H264, audio OPUS, echo cancellation built in, WebSockets NSURLSession
```

## 3. Signalling: `SylkRTC.py`

`SylkRTCConnection` is one `NSURLSessionWebSocketTask` per `wsServer` URL (`SylkRTCConnections`),
its delegate on the main queue, sub-protocol `sylkRTC-2`.

| Constant | Value | |
|---|---|---|
| `CONNECT_TIMEOUT` | 15 s | from opening to `ready-event` |
| `REQUEST_TIMEOUT` | 20 s | a request without `ack`/`error` fails |
| `PING_INTERVAL` | 30 s | WebSocket ping |
| `MAXIMUM_MESSAGE_SIZE` | 16 MB | a room's chat backlog can be large |

- `request(data, callback)` adds a `transaction`, queues until ready, calls `callback(error)`.
- Events go out as `SylkRTCConnectionGotEvent` (data.event), the end as `SylkRTCConnectionDidEnd`.
- `account-add` with the HA1 of the SIP credentials; the account is **never registered** through
  the gateway (a second registration would fork calls and messages to it).
- TLS certificate errors are ignored when SIP TLS verification is off.
- A connection nobody uses is released one second after its last room ends.

## 4. Media and the room: `Videoroom.py`

### 4.1 Session flow

```
account-add ─► publisher PeerConnection, offer ─► videoroom-join(offer, sip_instance)
  ◄─ session-state progress / accepted(answer) ─► set answer, limit the video bitrate
  ◄─ initial-publishers / publishers-joined ─► per publisher: videoroom-feed-attach
  ◄─ feed-attached(offer) ─► feed PeerConnection, answer ─► videoroom-feed-answer
  ◄─ session-state established (statistics start), feed-established
leave: videoroom-leave
```

ICE candidates go out as `videoroom-session-trickle` (end of candidates: an empty list), held back
until the request that creates their session on the server has been sent. `PUBLISHER_TIMEOUT`
(20 s) ends a join that never reaches *established*. Rooms of another server whose gateway
answers `SIP domain not allowed` are joined again as a guest (§6.3).

### 4.2 Media

One `RTCPeerConnectionFactory` for the process (`peer_connection_factory()`), with the default
encoder and decoder factories. libwebrtc calls its delegates and completion handlers on its
signalling thread; everything that touches the signalling or the UI is handed to the GUI thread.
Block signatures are registered with `objc.registerMetaDataForSelector` (offer, answer, the
descriptions, candidates, statistics).

- **Microphone and speakers**: libwebrtc's audio device (the system's default devices), with its
  own echo canceller; every feed's audio is played by it. Mute disables our audio track.
- **Camera**: the SDK's (`SIPApplication.video_device`) read through a `FrameBufferVideoRenderer`,
  like any other view of it, and handed to an `RTCVideoSource` as 32ARGB pixel buffers (a pool of
  IOSurface `CVPixelBuffer`s), so the camera chosen in Blink, the screen included, is what the room
  sees. "Camera off" disables the track (black is sent) and stops reading the camera.
- **Video profile** (`_apply_video_profile`), adapted in place when the device changes:

  | Device | Size | fps | Bitrate |
  |---|---|---|---|
  | a camera | 640×480 | 24 | 1024 kbit/s |
  | the screen ("My screen", `ScreenPointer.is_screen_device`) | 1280×720 | 15 | 1800 kbit/s |

  720p is the largest frame of H264 level 3.1 (`42e01f`), what the rooms negotiate. Going to or
  from the screen replaces the source (`_replace_video_source`): the screen gets a screencast
  source (`videoSourceForScreenCast:`, libwebrtc treats it as screen content) and the sender keeps
  the resolution and gives up frames (degradation preference *maintain resolution*); the new track
  is set on the same sender, without renegotiating, and `VideoroomSessionLocalVideoDidChange`
  tells the window.
- **Codecs** are negotiated by libwebrtc: the room's video codec is set on the server (Janus
  `videocodec`) and the answer picks one of those offered. libwebrtc announces its SSRCs and keeps
  the RTP header extensions, so the GStreamer workarounds of the Linux client are not needed.
- **Orientation (CVO)** of mobiles is applied by libwebrtc: frames come upright to the renderer.

### 4.3 Participants

`initial-publishers` / `publishers-joined` / `publishers-left` / `publisher-updated`.

| Publisher type | Subscribed | In the window |
|---|---|---|
| `webrtc` | yes | a tile with the picture |
| `bridge` (the SIP callers' mixed audio) | yes (audio) | none |
| `sip` (a SIP caller behind the bridge) | no | round avatar in the carousel |
| `sip` with our own address (our MSRP chat leg) | no | none (`is_self`) |
| `webrtc` whose feed offer has no usable video (`m=video` rejected, `inactive`, `recvonly`) | yes | round avatar in the carousel |

- **Name and picture**: those of the contact with that address in the contacts list (looked up
  once), else the room's display name, else the address.
- `Participant.video`: None until the feed's offer, then whether it carries video; False for SIP.
- A moderator's `mute-request` mutes our microphone.

### 4.4 Statistics: speaking and video flow

From the session established, every `STATS_INTERVAL` (0.3 s), `statisticsWithCompletionHandler:`
of the publisher and of every feed:

| From | Used for |
|---|---|
| publisher, `media-source` audio `audioLevel` | we speak (unless muted) |
| feed, `inbound-rtp` audio `audioLevel` | the participant speaks |
| feed, `inbound-rtp` video `framesDecoded` | `Participant.video_frames`: the window's video loss check |
| feed, `inbound-rtp` video `frameWidth` / `frameHeight` | `Participant.video_size`: 1280×720 or more is taken as a shared screen |
| feed, `inbound-rtp` video `bytesReceived` | `Participant.video_bytes`: the bitrate of the stats pill (and the loss check without frame counts) |
| feed, `codec` of the video `codecId` | `Participant.video_codec`, for the stats pill |
| `conference-audio-levels` event (`rx_peak`, the mixer's 0–255) | SIP participants speak |

Speaking is a level above −38 dBFS (`SPEAKING_LEVEL`, audioLevel is linear 0–1), or 25 on the
mixer's scale, held 0.6 s after the last loud one. When the room stops sending audio levels for
2 s, no SIP participant speaks. If the statistics cannot be read the log says so once and there
is no speaking highlight nor video loss handling.

### 4.5 Room chat over WebRTC

`send_chat_message()` (`videoroom-message`) and the `message` / `message-delivery` events, posted as
`VideoroomSessionGotMessage` / `VideoroomSessionDidDeliverMessage`; the gateway relays them to the
room's MSRP switch. Used by the chat window when its MSRP session to the room is not connected (§5.3).

### 4.6 Room events and requests

As Sylk Mobile's `react-native-sylkrtc`.

| Method | Request | Event back (`videoroom-event`) |
|---|---|---|
| `set_speakers(ids)` | `videoroom-configure` `active_participants` (0–2, ours = our session id) | `configure` {`active_participants`, `originator`}, to everyone, also on join |
| `toggle_hand([session_id])` | `videoroom-toggle-hand` | `raised-hands` {`raised_hands`} |
| `mute_everyone()` | `videoroom-mute-audio-participants` | `mute-audio` {`originator`}: the others mute; ours is ignored |
| `mute_participant(p, muted)` | `videoroom-mute-participant` | the WebRTC target gets `mute-request`; a SIP one is muted at the mix |
| `remove_participants(uris)` | `videoroom-remove` (SIP URIs) | — |
| `invite(uris)` | `videoroom-invite` (queued until established) | `invite-status` {`participant`, `state`, `code`, `reason`} |
| `refresh_video(p)` | `videoroom-session-update` video false, true 0.3 s later | — (Janus sends a keyframe) |
| `set_video_paused(paused)` | `videoroom-session-update` per feed | — |
| join | `videoroom-join` + `sip_instance` (not as a guest) | — |

### 4.7 Notifications (sender the VideoroomSession)

| Name | Data |
|---|---|
| `VideoroomSessionDidChangeState` | state, reason, room |
| `VideoroomSessionAudioMutedDidChange` / `…VideoMutedDidChange` | muted, room |
| `VideoroomSessionParticipantDidJoin` / `…DidLeave` / `…DidChange` | participant |
| `VideoroomSessionParticipantsDidChange` | — |
| `VideoroomSessionParticipantSpeakingDidChange` | participant |
| `VideoroomSessionSpeakingDidChange` | speaking (ours) |
| `VideoroomSessionLocalVideoDidChange` | track (ours, replaced: the screen or the camera) |
| `VideoroomSessionSpeakersDidChange` | speakers, originator (a name, '' or 'you') |
| `VideoroomSessionRaisedHandsDidChange` | raised_hands |
| `VideoroomSessionWasMutedByOther` | originator |
| `VideoroomSessionGotInviteStatus` | uri, state, code, reason |
| `VideoroomSessionGotMessage` | type, content, content_type, sender_uri, sender_name, timestamp |
| `VideoroomSessionDidDeliverMessage` | message_id, delivered, code, reason |

## 5. User interface

### 5.1 Join Conference panel (`ConferenceController.JoinConferenceWindowController`)

- Media: **Chat** (always), **Audio**, and **Video**, shown only when the account's domain runs
  SylkServer and the media can be done here, or for a foreign SylkServer room (§6).
- **Video** checked: audio and video over WebRTC (Audio unchecked: the microphone starts muted),
  chat over MSRP. **Audio** only: SIP audio and chat to the bridge, as before.
- The room field takes a room name, `room@host` or a link `https://host/conference/room`; rooms of
  another domain are looked up 0.4 s after the typing stops and **OK waits for the answer**.
- The room field is a combo box (the xib's text field replaced in code, `_makeRoomComboBox`) of
  the rooms joined before: as typed, most recent first, at most 20, saved in the user defaults
  (`ConferenceRecentRooms`) when OK passes validation. A room picked from it is looked up as a typed
  one. The saved configurations popup is unchanged.
- Bottom line: "Conferencing provided by SylkServer at <conference domain>", "Looking up the
  SylkServer of <host>…", or "No SylkServer found for <host>: the room is joined over SIP".

`ContactWindowController.joinConference` does the join for the panel, a video call to a room
contact, a redial of a room and the video window's Chat button; it returns the session controller.
`joinVideoConference` returns (the media left for SIP, whether the video room was joined). The
chat session of a room joined with video is marked `opened_for_videoroom` and
`open_chat_window_minimized` (§5.3).

### 5.2 Video conference window (`VideoroomWindowController.py`)

Built in code (no xib). The content view and the tiles are flipped; the tiles take no clicks, the
content view takes every click and mouse move and finds the tile under it.

**Grid** (layout in §7). A tile is an `RTCMTLNSVideoView` in a container (which mirrors our own
camera) and an overlay view that draws everything else. Pictures are cropped to fill their tile;
our own shared screen is fitted, not mirrored.

**Name pills** at the tiles' outer corners: top for the top row, bottom for the others, left or
right by side, centred for a middle column; a pill that would be under the call bar or our floating
thumbnail is moved above it.

**Badges on a tile**: a yellow raised hand (the corner beside the name, same edge), a green border
while the participant speaks (on our own tile only when someone else is in the room), the stats
pill under the mouse (codec · kbit/s, top middle), "weak video" over the last picture while no new
frames come, the initial with "connecting…" or "no media" while there is no picture at all.

**Call bar**: the video call window's `VideoCallBar`, at 75 % (`scale`, drawn smaller through its
bounds) and lifted above the bottom row's name pills (`bottom`).

| Segment | |
|---|---|
| Mute | microphone |
| Camera (menu) | System Default and each camera, the screen (one entry per display) while connected, Turn camera off/on; red "Camera off" while off |
| Stop sharing | red, only while the screen is shared |
| Speakers | the speaker sheet (§5.6); with four people or more, or speakers set |
| Chat | the room's chat window in front (deminiaturized), the chat started if there is none |
| Raise/Lower hand | with three people or more (or our hand still up); yellow while up |
| More (menu) | Share screen / Stop sharing screen, Hide myself / Show myself, Show me as a thumbnail / Put me back in the grid, Invite participants…, Share conference address…, Mute everyone, Lower all hands |
| Full screen / Window | |
| Leave | |

The bar hides after 5 s without mouse movement, with the carousel, and comes back when the mouse
moves. A double click outside our tile toggles full screen; Esc cancels a pick, then leaves full
screen.

**Carousel** (above the call bar, no background): what is not in the grid — past the grid's 8,
audio only participants, those demoted after losing video, and, with speakers set, everyone but the
speakers. Video thumbnails 80×45 cropped, name as tooltip, a red mic when muted; audio participants
as round avatars (`Avatars.draw_avatar`: the contact's picture, or initials on its colour) with a
green ring while speaking and a hand badge. The wheel or the carets at its ends scroll it.
**Pick and replace**: a click picks a video thumbnail (blue border), the grid's remote tiles show
"Replace", a click on one swaps them (the picked one is pinned: video loss never demotes it); a
second click, a click elsewhere or Esc cancels. Not while speakers are set.

**People in the room** decide the room's controls: distinct addresses (`aorCount`: devices of one
person count once), us included, the audio bridge and our own MSRP leg left out.

| People | Shown |
|---|---|
| 3 or more | Raise/Lower hand, Lower hand on a tile, Lower all hands, Mute everyone, Mute/Unmute on a tile |
| 4 or more | Speakers, and on a tile Show as the speaker / as second speaker |
| fewer | hidden, except a hand still up (Lower hand) and speakers still set (Speakers, Stop showing as speaker), so they can be cleared |
| any | Remove from the conference on a SIP participant's tile (there is a tile: we are not alone) |

**Right-click on a tile**: Mute/Unmute, Lower hand, Show as the speaker / as second speaker / Stop
showing as speaker, Remove from the conference (SIP participants), as the table allows. On ours:
Show me as a thumbnail / Put me back in the grid, Hide myself, Raise/Lower hand, the speaker items.

**Our own tile**: a round button (`VideoroomThumbnailButton`, a real button over the tile, shown
and hidden with the call bar) moves it between the grid and the thumbnail; it takes the first
corner of the tile clear of the name pill and the hand badge. It is there when someone else's video
is in the grid and no speakers are set. The thumbnail (20 % of the width, 160–320 px, 16:9) can be
dragged and snaps to the nearest corner. *Hide myself* (connected, with someone else's video to
show) removes it from the window only.

**Video loss**, checked once a second. The bitrate alone says nothing: a shared screen that does
not change encodes to almost nothing and is still fine, so what counts is new frames
(`framesDecoded`; video bytes when the frame count is not known):

| | |
|---|---|
| degraded | no new frames for 4 s, after a 5 s warm-up: the tile keeps its place and its last picture, with a "weak video" mark |
| demoted | degraded for 30 s: to the carousel (not a pinned tile, not a speaker, not a shared screen) |
| recovered | new frames; a demoted tile returns to the grid |
| stalled | no new frames for 20 s: `refresh_video` once (not a shared screen) |
| shared screen | for now guessed from the frame size (1280×720 or more, Blink sends cameras at VGA); to be replaced by a flag from the room (§9) |

The stats pill shows codec · kbit/s (the EMA of the received bitrate, α 0.4) · "weak video".

**Pausing**: 2 s after the window cannot be seen (occlusion: minimized, another Space, fully
covered) every feed's video is paused at the server, audio going on; resumed when visible. The video
loss check is suspended meanwhile.

**Screen sharing**: More → Share screen or the Camera menu selects the screen as the video device
(`ScreenPointerController.select_video_device`, which also sets the screen capture resolution);
the device in use before is remembered. While shared: our tile is hidden, the window minimizes
(leaving full screen first), and, with no speakers chosen, we become the room's single speaker.
When it stops (Stop sharing, the Devices menu, or the capture ending) the camera comes back, the
speakers go back to the grid if we set them, our tile comes back and the window is restored as it
was (full screen again if it was). Closing the window while sharing puts the camera back.

**Status pill**: while connecting, why the room ended, and notes for 3 s: "X raised a hand", "Muted
by X", "Speakers set by X", "Inviting X", "Calling X…", "X answered", "X: reason (code)" (5 s),
"Sharing your screen", "Your video is hidden here…". Window title: `Room <name> · <n>
participants · mm:ss` once connected.

**Inviting**: More → Invite participants… (the Add Participants panel), or contacts dragged from
the contact list onto the window (`x-blink-sip-uri`).

### 5.3 Chat window, for a room with a video room

| Control | Behaviour |
|---|---|
| Toolbar Audio / Video | only start what is not there or bring up what is; Audio and Video bring up the video window while a video room is open |
| Toolbar Video | joins with audio and video over WebRTC, which removes the session's SIP audio; for rooms that can have video, a foreign SylkServer room included before its `isfocus` is known |
| Toolbar Audio | removed while the video room is up (Chat and Video only), put back when it ends |
| Microphone | mutes the video room, in step with its window and a moderator |
| Hang up | ends the chat and leaves the video room |
| Input bar | enabled when the MSRP chat is connected, or the video room is (the chat then goes over WebRTC) |
| Participants drawer | dropping contacts invites them: REFER over MSRP, else through the video room; the audio bridge (`;app=…bridge` entity) is not listed; our own row shows audio and video icons while the video room is up |
| Opening | a room joined with video opens its chat window minimized (`ChatController.openChatWindow`), or leaves it as it is when it is already on screen; the video window's Chat button brings it to the front |
| Ending | the chat Blink opened for the video room (`opened_for_videoroom`: Join panel with Video, a video call to a room, the video window's Chat button) ends when the video room ends, however it ends; a chat that was there before (escalated with Video) goes on |
| Hangup tone | not played when the SIP audio ends because it moved to the video room (`silent_media_end` on the controller, `blink_silent_media_end` on the SIP session, read by `SessionRinger`) |

Room chat over WebRTC (`ChatController.updateVideoRoomChat`): when the MSRP chat is not connected
and the video room is established, a `VideoroomChatStream` stands in for the chat stream: messages
go with `videoroom-message`, the room's come in as `ChatStreamGotMessage` and are kept in history
like MSRP ones; our own echoed messages are skipped. When the MSRP chat connects it carries the chat.

### 5.4 Share sheet (`ConferenceShareDialog.py`)

As Sylk Mobile's ShareConferenceLinkModal, from the configuration of the room's own domain: Web
`<publicUrl>/conference/<room>`, SIP `<room>@<conference domain>`, PSTN number and room number when
`conference.pstnBridge` is set, and the QR code of `sylk://<domain>/conference/<room>`
(CIQRCodeGenerator). Copy and E-mail. Opened from More → Share conference address….

### 5.5 Incoming invitation (`AlertPanel.py`)

An INVITE from a conference focus (`remote_focus`, or a conference address by `is_conference_uri`)
that carries `Referred-By` (the focus copies it from the inviter's REFER) shows as an invitation
from that person: title "Conference Invitation", their name and picture from the contacts (else the
name in the header, else the address), the line "invites you to conference <room>". Accepting is as
before: the session is the room's.

### 5.6 Speaker selection (`SpeakerSelectionPanel`, in `VideoroomWindowController.py`)

A sheet as Sylk Mobile's SpeakerSelectionModal: Grid / 1 speaker / 2 speakers, a popup per slot
with the participants with video ("Me" first, the others by name), one person in one slot only.
Apply sends `videoroom-configure`; the window changes when the room's `configure` event comes back
(whoever set it). One speaker fills the window, two side by side, everyone else in the carousel.

## 6. Connecting to foreign domains

### 6.1 What a foreign room is

A room whose host is neither the account's conference server nor in its domain
(`WebRTCSupport.local_room_host`), given as `room@conference.example.com`, `room@example.com` or a
link `https://example.com/conference/room` (`parse_room`). Its SylkServer is found by the same
discovery as for accounts, for the room's domain: `conference.` and `videoconference.` are
stripped from the host and `_sylkserver.<domain>` is looked up.

| From the configuration | `RoomTarget` |
|---|---|
| `wsServer` | `ws_server` |
| `iceServers` | `ice_servers` |
| `defaultConferenceDomain` (default `videoconference.<domain>`) | `video_uri`: `room@videoconference.<domain>` |
| (convention) | `sip_uri`: `room@conference.<domain>`, the MSRP switch |
| `defaultGuestDomain` (default `guest.<domain>`) | `guest_domain` |

`remote` is true when its `wsServer` is not the account's own.

### 6.2 Discovery

`resolve_room()` uses the cached configuration, or discovers it (up to 12 s, `DISCOVERY_TIMEOUT`,
polled every 0.2 s), then calls back in the GUI thread. The Join panel keeps OK disabled until then.

### 6.3 Identity

The gateway authenticates nothing on `account-add` and `videoroom-join`: it checks the domain
against its `sip_domains` and the room's policy. Blink joins as its own address first; when the
server answers `SIP domain not allowed` it joins once more as a **guest** of that server:
`<display name><4 hex>@<defaultGuestDomain>`, a throwaway HA1, the display name kept, no
`sip_instance`.

### 6.4 Chat with a foreign room

The MSRP chat goes over SIP to `room@conference.<domain>`, through our proxy and theirs. When it
cannot connect, the chat goes over the video room (§5.3). MSRP failures are explained in the
Activity log (`MSRPDiagnostics`).

What the foreign server needs (OpenSIPS relay of its conference domain, `[MSRP] msrp_hostname`,
the shared `[MSRP] port`, a real certificate, the Contact kept with `isfocus`) is in §6.5 of the
Linux client's document; it is server configuration and serves both clients.

## 7. Tile layout (`VideoroomWindowController.layoutTiles`)

| In the grid | Layout |
|---|---|
| 1 | the whole window |
| 2 | side by side; one over the other when the window is at most as wide as high (`TWO_IN_A_COLUMN_ASPECT` 1.0) |
| 3 | 3×1 when width/height ≥ 2.2, 1×3 when ≤ 0.75, else 2 over 1 |
| 4–8 | two rows, the longer on top, a shorter bottom row centred: 2×2, 3+2, 3×2, 4+3, 4×2 |

- **Our own video** floats as the thumbnail when that leaves an even grid of the others (3 of us:
  2×1 and the thumbnail, 5: 2×2, 7: 3×2, 9: 4×2), or when the others fill the grid; with an even
  number of us it is in the grid. Its button or menu overrides this (floating with one other or
  more, or in the grid), only with others with video and no speakers set.
- **At most 8** in the grid; the others, audio only participants and demoted ones go to the
  carousel. Speakers set: the speakers alone (one, or two side by side).
- Tiles take the whole cell. Ours first, the others in joining order (pick and replace swaps them).

## 8. Logging

| Tag | What |
|---|---|
| `[webrtc]` | availability, the framework, the lookup of a foreign domain |
| `[sylkrtc]` | connections, accounts added, failed requests |
| `[videoroom]` | join, offer and answer (summary; full SDP at debug level), participants, ICE (debug), speakers set (and by whom), hands raised, muting, invite progress, `video lost (no new frames for N s[, a shared screen])` / `moved to the carousel` / `video back` / `video stalled`, pausing and resuming feeds, `sending the screen/camera at …`, statistics not readable |
| `[conference]` | joins, SIP audio removed for WebRTC, invitations (`X invites us to Y`) |
| `[screen sharing]` | sharing started and stopped from the window, the camera back |
| `[msrp]` | why an MSRP stream did not start |

## 9. State, limits and open items

**Not tested yet** (the 2026-10-25 port), most likely to need work:

- the statistics block (`statisticsWithCompletionHandler:` metadata, the report's dictionaries):
  without it there is no speaking highlight and no video loss handling;
- the chat window opening minimized when it was not on screen (`orderBack_` + `miniaturize_`);
- where the Camera and More menus pop up over the 75 % call bar;
- the coloured SF Symbols on the badges (palette configuration, macOS 12+);
- minimizing and restoring the window around screen sharing from full screen;
- the speaker sheet (built in code);
- replacing the video source when the screen is shared (`videoSourceForScreenCast:`, `setTrack:`);
- the room field turned into a combo box at run time (key view loop, delegate).

Open:

- Who shares a screen is guessed from the frame size; a mobile sending its camera at 720p passes as
  a screen (never demoted). A flag from the room (the sender says so, the gateway passes it on, for
  example in `publisher-updated`) needs sylkserver and Sylk Mobile/desktop.

Limits:

- Audio uses the system's default devices (libwebrtc's audio device), not Blink's audio settings.
- Outgoing camera video is 640×480 at 24 fps; the screen 1280×720 at 15 fps (H264 level 3.1).
- No composing indications or file transfers over WebRTC; the room's `file-sharing` event is not
  shown; no conference recording.
- The speaking thresholds (−38 dBFS, 25 on the mixer's scale) and the video loss threshold
  (80 kbit/s) are the Linux client's untested guesses; a nearly still picture can dip under 80 kbit/s.
- The chat window has tabs, not session tiles: only our participant row shows audio and video while
  the video room is up.
- Opening the chat of a room joined with video selects its tab even when the window is in use.

## 10. Server and SDK requirements

From the Linux client's §10.5–10.6, deployed once for both clients:

- webrtcgateway: `videoroom-join` takes `sip_instance` (older gateways ignore it); the REFER of a
  WebRTC user's invite carries `media="audio,chat"` for SIP addresses, `media=audio` for numbers.
- conference: an audio stream that times out ends the session even when it also has chat.
- python3-sipsimple: `Session.connect(..., contact_parameters=...)`, used for `+sip.instance` on the
  INVITE to a room (`SessionController`), so the gateway knows our MSRP leg is this device and does
  not show us twice or, after we leave the video room, as a SIP caller; the RTP timeout fix.
