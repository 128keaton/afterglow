# Harmony 880 and 885 flash images

The first 128 KiB of an 880's flash hold two programs, a bootloader at `0x000000` and the
application at `0x010000`; the user's configuration follows at `0x020000`
([tptp.md](tptp.md)) and a new application is staged at `0x1D0000`. The addresses are
libconcord's arch 8 `flash_base`, `firmware_base`, `config_base` and `firmware_update_base`.

**The application can be written again over USB whenever the bootloader runs. Nothing can write
the bootloader back if it is destroyed**, so an image offered for `0x000000` is held to the
strictest check in the package. `backends/harmony_880/firmware.py` makes these checks and writes
nothing to a remote.

Sources for what the bootloader and application do are
[@dannybloe](https://github.com/dannybloe)'s
[harmony-explorations](https://github.com/dannybloe/harmony-explorations),
`reference/architectures/harmony-880-885/firmware.md`, read from published images. They are cited,
not re-derived. What was checked here is that the images supplied with this work agree with them.

## The application

A 64 KiB image of which bytes up to `0xFB88` are programmed and the rest erased.

| Offset | Bytes | Meaning |
| ---: | --- | --- |
| `0x00` | 2 bytes | checksum |
| `0x04` | `HG` | magic |
| `0xC00` | a `GOTO` | the entry point the bootloader jumps to |

The checksum is the configuration's: two lanes seeded `0x21` and `0x43`, but over the even and odd
bytes **from offset 4** to the end of the 64 KiB. It holds on both applications supplied here.
The bootloader checks it at every power-on where no key is held, and stays resident if it fails.

The USB product string of the Spare-4 application is `Harmony Remote 0-4.4.2` (UTF-16, at
`0xA604`): the software type, a dash, the version.

## The bootloader

A 64 KiB image of which about the first 25 KiB are programmed. Its reset vector is a `GOTO` to
code near the end of what is programmed (`0x637C` on the Spare-4 remote's), and its two interrupt
vectors, at `0x08` and `0x18`, are `GOTO`s into the application, at `0x010400` and `0x010800`. It
carries the UTF-16 string `Harmony Bootloader`. It has no checksum.

## What was supplied

Read here with `firmware.check_application` and `firmware.check_bootloader`:

| File | Verdict |
| --- | --- |
| `H880-Spare-4-Firmware.bin` (kept beside the supplied files; named as read from the Spare-4 remote) | a valid application; `Harmony Remote 0-4.4.2` |
| `H880-Spare-4-Safemode.bin` (the same, its bootloader) | a valid bootloader: reset to `0x637C`, vectors to `0x010400` and `0x010800`, programmed to `0x639B` |
| `firmware.EZUp` (one `Firmware_Main` phase) | a valid application by checksum and magic. **A different build from the Spare-4's:** it differs from it in 37,722 of 65,536 bytes, has no `Harmony Remote x-y.z` product string, and carries the UTF-16 string `HarmonyRemote.com!` at `0xA482`. Its version, and whether it is for skin 15, are not established |
| `safe.bin` | **not a usable bootloader** - below |

### `safe.bin` must never be written to `0x000000`

It is 64 KiB, starts with the same reset `GOTO 0x637C` as the Spare-4 bootloader, and is
programmed only up to `0x6353`. **Nothing is programmed at the address it jumps to**: the remote
would execute erased flash from reset, and with the bootloader being the program that runs at
power-on and the only one that can write the application back, it would not start and could not be
recovered. In addition:

- both interrupt vectors, at `0x08` and `0x18`, are erased, where the Spare-4 bootloader's hand
  off to the application;
- 21,390 bytes differ from the Spare-4 bootloader, including most of the code from `0x0400` to
  `0x6000`;
- it carries no `Harmony Bootloader` string, but fragments of German, Spanish and French message
  text (`Unerwarteter Fehler`, `Error Inesperado`, `Message D Erreur` appear whole in the
  applications; in `safe.bin` they are interleaved and broken).

What it is - a truncated read, a damaged region, or an image from something else - is not
established. It differs from every known bootloader and fails `firmware.check_bootloader`, which
is as far as this goes. `safe.zip` holds the same file.

## Not known

- The application's version in `firmware.EZUp`, and whether the 880's configuration written under
  one build runs under the other.
- What the remote does in the bootloader with a configuration present, and whether Afterglow's
  link can identify a remote that is in it: no remote was attached here.
- The bootloader's and application's behaviour beyond what the cited reference reads.
