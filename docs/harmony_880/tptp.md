# Harmony 880 and 885 `TPTP` configuration image

The Harmony 880 (architecture 8, skin 15, board 1.8.0) keeps its configuration in an image that
begins `TPTP` and ends `DKDK`, carried in the same XML-header EZHex envelope as the other direct
Harmony remotes ([ezhex.md](../harmony_pk/ezhex.md)). It is a sibling of the Harmony One's `GSPM`
image ([gspm.md](../harmony_one/gspm.md)): the same two-lane checksum, the same absolute-pointer
index, an older marker. This page records what is established about the container and what the
remote does when it is wrong. **It says nothing about what the sections mean.** Afterglow reads an
image's devices and their infrared commands through a vendored copy of the community's decompiler
(`backends/harmony_880/decompiler/`); what that reads is described in harmony-decompiler's
`docs/FORMAT.md`, not here.

## What this was measured on

Sixteen intact images, read with the checks in `backends/harmony_880/tptp.py`: eleven 880 images
(skin 15, board 1.8.0; two supplied with this work, `H880-Spare-4.EZHex` and `Update.EZHex`, and nine
published as samples by other people), four 885 images (skin 17, board 2.1.0) and one 785 image
(skin 45, board 0.8.0). The published samples are
[harmony-decompiler](https://github.com/trelowney/harmony-decompiler)'s `samples/arch8/`. Two more
images supplied with this work are damaged and are described below.

Everything under "Layout" held in all sixteen. The 885 and 785 images differ from the 880's only in
the marker's last byte (below); they are listed so that nobody takes the 880's profile to cover
them: they are not covered.

## Layout

Integers are little-endian. Pointers are absolute: the image is mapped at `0x020000` (libconcord's
arch 8 `config_base`), so a pointer minus `0x020000` is an offset into the image.

| Offset | Bytes | Meaning |
| ---: | --- | --- |
| `0x00` | `TPTP` | cookie |
| `0x04` | `u32` | absolute address of `DKDK`; the top byte is 0 |
| `0x08` | `u32` `0x1500` | format word; the Harmony One's is `0x1600` |
| `0x0C` | 20 x `u32` | section pointers, slots 0 to 19 |
| `0x5C` | 3 x `00` | zero |
| `0x5F` | `WLWL` | marker |
| `0x63` | `u8` | `0x38` in every 880 and 785 image, `0x3A` in every 885 image; meaning not known |
| end - 6 | 2 bytes | checksum |
| end - 4 | `DKDK` | closing marker, which the end vector points at |

Nothing follows `DKDK`; the end vector is `0x020000 + (length - 4)` in every image. The trailer
is a function of the rest of the image and nothing else: closing the body of each of the sixteen
again with `tptp.seal` gives back its bytes exactly.

**Slots 8 and 19 are empty in every image and the other eighteen are filled.** The Harmony One's
index leaves only slot 8 empty. The eighteen pointers are all different and are **not in ascending
order**, so a section's extent is the distance to the next higher pointer, not to the next slot.

The checksum is the One's: two lanes seeded `0x21` and `0x43`, the even bytes of the image from
offset 0 up to the checksum into the first and the odd bytes into the second. **A last byte with no
partner is left out.** Three images have an odd length before the checksum and all three verify that
way; one of them (the 785's) holds a non-zero byte there, which is what separates this rule from
"the byte happened to be zero". The container header's `CHECKSUM` is `0x69` XORed over the payload,
as in [ezhex.md](../harmony_pk/ezhex.md), and held in all sixteen.

## What the remote does with a wrong image

At every start the application checks the built-in configuration, the identity block, and then the
user's configuration, and runs the user's only if it passed. The check reads four things:

1. the cookie `TPTP` at the base,
2. the marker `WLWL` at base + `0x5F`,
3. `DKDK` at the address the three bytes at base + 4 state,
4. the two bytes before `DKDK` against the checksum above.

Nothing else is compared: not the format word, not a section pointer, not the architecture or skin
in the envelope. An image can pass and still misbehave, and the layout above is checked by
Afterglow on top of the start check for that reason (`tptp.validate`). The check cannot be too
strict against the firmware's and must not be looser, so the two are separate functions.

A failure does **not** brick the remote. The application shows a message and runs its own built-in
configuration; it still answers over USB, and the configuration can be written again:

| Message | When | Text |
| ---: | --- | --- |
| `0x00` | cookie, marker or `DKDK` wrong | "Go to Website to update settings" |
| `0x1A` | markers right, checksum wrong | "Configuration Corrupted" |

The verdict is recomputed at every start and does not latch.

Source: [@dannybloe](https://github.com/dannybloe)'s
[harmony-explorations](https://github.com/dannybloe/harmony-explorations),
`reference/architectures/harmony-880-885/firmware.md`, read from the 880/885 application image.
That reading has not been re-derived here; what was checked here is that the sixteen intact images
pass it and the two damaged ones fail it, below.

## Two damaged images

Both are `Update.EZHex` with a label edit applied. The supplied `test.log` records the edit
(`'TV' -> 'Butt'`, `'Watch' -> 'Watch Butt'`); that this is how they came about is read from that
log and from the differences below, not known. Offsets are into the image.

| File | Differences from `Update.EZHex` | The remote would show |
| --- | --- | --- |
| `my-remote-broken2.ezhex` | the u24 pointer at `0x1BE5A` was retargeted from `0x03BE4E` to `0x09A482`, which is offset `0x7A482`, **where the checksum and `DKDK` were**; six bytes of text (`05 03 0a 31 16 1a`) were written over them; the byte at `0x2404E` went from `04` to `00`. The length is unchanged | `0x00`: no `DKDK` |
| `fucked.ezhex` | at `0x18DBD`, `0x1995F` and `0x1BEA5` a byte went from `05` to `04` and the bytes after it were replaced by a pointer - `0x09A47E`, `0x09A487`, `0x09A495` - and, in two places, a `14` and another pointer: what reads as inline text turned into text by reference (not decoded here). 28 bytes of new text were put where the trailer was, starting at offsets `0x7A482`, `0x7A48B` and `0x7A499`, so each pointer falls exactly four bytes short of a string, the length of `DKDK`. A new checksum follows the text and `DKDK` was never restored. The end vector moved 24 bytes on and points at `15 00 84 b0` | `0x00`: no `DKDK` |

Both carry a valid EZHex `CHECKSUM`. That is the envelope's, over the payload as it stands, and
says nothing about the image inside: it was computed over a payload that was already wrong.
`tptp.seal` is the one place a length-changing writer should close an image: it sets the end
vector, the checksum and `DKDK` together.

## A run of damaged backups

`~/Documents/Afterglow/Dumps` on the machine the 880 was attached to held 76 backups, each the
remote's configuration read back just before a write. Eleven fail `tptp.validate`: three on the
checksum and eight with no `DKDK`. One of the eight, the same image in five backups in a row,
was on the remote through five writes, each logged "written and verified": libconcord's
verification is a readback that equals what was sent, and says nothing of whether the remote
will start it. The first 880 backend built from the newest file in that folder, so a damaged
image read back from the remote became the base of the next build; it now builds from the image
the project was imported from, and refuses one that fails `tptp.validate`.

## Checking a file

    python3 tools/harmony_880_check.py config.ezhex [firmware.EZUp] [image.bin] ...

It reads only the files it is given, prints what the remote would make of each, and exits 1 if any
would be refused. Run it on a file before anything is written to a remote.

## Not known

- What any section holds. The community's reading of arch 8 is in harmony-decompiler's
  `docs/FORMAT.md` and harmony-explorations' `reference/`; none of it is used here.
- The marker's last byte.
- Whether an image that passes `tptp.validate` and differs from every known one would run.
- Whether the 880 and 885 layouts differ in anything but the skin: the 885 was seen only in four
  published images, none read off hardware here.
