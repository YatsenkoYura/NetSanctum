# NetSanctumOS for CC:Tweaked

NetSanctum's optional `computercraft` module exposes active product modules through versioned
integration contracts and serves the NetSanctumOS Lua client. The computer terminal is always the
control panel. An attached monitor or monitor wall is display-only, and all attached speakers are
used as audio outputs.

## Server

The default Docker build includes `computercraft`. Rebuild the node after updating:

```bash
./start.sh --restart
```

The module discovers every active provider of the shared `library.viewer.v1` contract. Current
provider IDs are:

- `music.library.viewer.v1`
- `video_archiver.library.viewer.v1`
- `alllib.library.viewer.v1`

Disabled or uninstalled providers disappear from NetSanctumOS automatically. A future module only
needs to implement the shared contract; ComputerCraft does not maintain a module allowlist. The
common API is under `/api/computercraft`; media conversion, CC palette rendering, and DFPWM encoding
belong only to this module. Provider storage paths are resolved through an internal resource handler
and are never included in the public integration JSON.

## Install

The node serves the client without authentication so a fresh computer can install it directly:

```text
wget https://netsanctum.example.net/computercraft/client.lua netsanctum
netsanctum
```

Alternatively, download the repository file
`app/modules/computercraft/netsanctum_os.lua`. On first launch, enter the node base URL and owner
token. NetSanctumOS stores them in `.netsanctum-os.cfg` and migrates the previous
`.netsanctum-video.cfg` automatically.

Use a specific monitor or force the internal terminal with:

```text
netsanctum left
netsanctum monitor_12
netsanctum terminal
netsanctum reset
```

## Runtime

Boot initialization reports the controller, monitors, selected display, speakers, authentication,
and active modules. The computer uses keyboard and mouse events for all navigation. The monitor is
reserved for previews, video, manga pages, and novel text.

Current viewers:

- Music: catalog and DFPWM speaker playback.
- Video Archive: catalog, terminal frames, DFPWM audio, pause, and seek.
- Lib Network: catalog, chapter selection, novel reader, manga viewer, and anime playback.

Video and image requests select their terminal resolution dynamically. DFPWM is mono at 48 kHz and
is broadcast to every attached speaker.

Remote or encrypted media is materialized once into a private seekable cache under `/tmp` so frame
requests do not download the object repeatedly. Individual cached media is limited to 512 MiB and
the process cache is limited to 1 GiB.
