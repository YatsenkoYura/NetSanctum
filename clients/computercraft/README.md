# NetSanctum Video for CC:Tweaked

`netsanctum_video.lua` is a terminal client for the NetSanctum Video Archiver API. It lists completed
videos and asks the server for frames matching the current terminal or multiblock monitor size. It
does not use the web dashboard or HTMX.

## Requirements

- NetSanctum built with the `video_archiver` module and this version of the server code.
- A CC:Tweaked advanced computer or monitor with the HTTP API enabled.
- An optional speaker peripheral for synchronized audio.
- Network access from Minecraft to the NetSanctum node. Add the node host to CC:Tweaked's HTTP
  allowlist when the server configuration blocks private or plain-HTTP addresses.
- The persistent NetSanctum owner token. The client exchanges it for a 24-hour bearer session.

## Install and run

Put `netsanctum_video.lua` on the computer as `netsanctum_video`, then run:

```text
netsanctum_video
```

On first run, enter the node base URL, for example `https://netsanctum.example.net`, and the owner
token. The token is masked while typing and stored in `.netsanctum-video.cfg` on that computer.
Treat the computer and its disk as trusted. To remove the saved settings:

```text
netsanctum_video reset
```

The client automatically uses the first attached monitor and sets its text scale to `0.5`. A joined
monitor wall is exposed by CC:Tweaked as one peripheral and uses its full dimensions. To select a
specific local side or wired peripheral name, pass it as the first argument:

```text
netsanctum_video left
netsanctum_video monitor_12
```

Use `netsanctum_video terminal` to keep output on the computer even when a monitor is attached.
The first attached speaker is detected automatically.

Controls in the catalog are Up, Down, Enter, R, and Q. On an advanced monitor, touch a video to play
it or touch `[X]` to exit. During playback use Space to pause, Left/Right to seek 10 seconds, N/P to
change video, and Q to return. The monitor's bottom row provides Back, Previous, seek, pause, and
Next touch controls. Playback is intentionally limited to about two frames per second.

## Frame API

The client uses the general authenticated frame endpoint:

```text
GET /api/video-archiver/videos/{id}/frame
    ?time=12.5
    &width=120
    &height=60
    &format=cc-palette
    &fit=contain
```

Supported formats are `cc-palette`, `nfp`, `png`, `jpeg`, and `webp`. Supported fit modes are
`contain`, `cover`, and `stretch`. `cc-palette` returns JSON with one string of CC blit color digits
per row; `nfp` returns the same pixels as a paintutils-compatible text image. Other formats return
the encoded image directly.

Dimensions are selected per request. Image formats allow up to 2,073,600 pixels per frame;
CC palette formats allow up to 262,144 cells, so joined monitor walls such as `5x5` are supported.

For audio, the client requests the existing video audio endpoint with `format=dfpwm&time=<seconds>`.
The default remains `format=mp3` for browser clients. DFPWM is mono at 48 kHz, as required by
CC:Tweaked's `speaker.playAudio` API.
