# Harmony 880 infrared commands

How an 880 image keeps a command's infrared, and how Afterglow reads and writes it
(`backends/harmony_880/ir.py`). The walk to the records is the community's
(`decompiler/ir_reader.py`, from harmony-decompiler); what is written here is what held
when it was read against the images, and where it was not put on a remote, it says so.

## Where a command is

Section 5 is a table of IR groups, one for each device. A group is a spare byte, a `u16`
count and a `u24` pointer to each of its records. A record is:

| Offset | Bytes | Meaning |
| ---: | --- | --- |
| 0 | `00` | spare |
| 1 | `u24` | the carrier's period in nanoseconds |
| 4 | `u24` | the time the carrier is on in each period, in nanoseconds |
| 7 | `01` | class: a stream of durations |
| 8 | `u24` | the record's own start address |
| 11 | `u8` | the number of sets, 1 to 16 |
| 12 | 3 x `u24` for each set | the addresses of the set's **press**, **hold** and **release** blocks, 0 for none |

The group's pointer to a record is the address of its class byte, seven bytes into the record.
Of 257 records in `Update.EZHex`, 254 have that one pointer and no other; the rest have
byte patterns that match by chance. Every block of a record lies below it.

A **set** is what one press sends. A command that toggles (RC5, RC6) has two, and the remote
sends them in turn. The **press** block is sent when the key goes down, the **hold** block
again while it is held, the **release** block when it is let go. Five images hold 1,844
press blocks, 1,012 holds and 117 releases (116 of them in one image).

## A block

`u16` words ended by a zero word. A mark has the top bit set and the rest is its length in
microseconds; a space is its length, up to 32767. A longer space is several words: a press
starts with a lead-in space of 50,000 to 100,000 microseconds (two or more words, the first
`0x7FFF`) and ends with a one-microsecond close word, which is folded into the last space when
the block is read. A hold or a release has no lead-in. Several words in a row of one polarity
are one pulse, and a reader that merges them cannot tell how they were split, so a block read
and written again reads back as the same pulses, not always as the same bytes.

This is the encoding the Harmony One and 800 use, and `ir.py` is the same rule as theirs.

## What is read

`ir.decode` turns a record into a portable waveform signal: the carrier, the press (lead-in
removed, close word removed) followed by the hold, `sections` saying where one ends, a second
set as the signal's `alternate`. `signal["native"]` keeps where it came from (`group`, `record`),
the lead-ins and the carrier's nanoseconds, so that it can be written back as it was. A record
that does not read in that shape (a release that does not begin with a mark, in one image) is
left as it is and imports as a rough waveform that cannot be written.

Of the 1,833 records in six images, every one reads except one in `H880-Spare-4.EZHex`.

## What is written

`ir.replace` writes a changed command by **adding**: its blocks, then a new record, at the end
of the image, and then pointing the group's entry at the new record. A record's blocks must be
below it and nothing but the group's entry points at a record, so nothing that is there moves
and what the old record pointed at is left, unreferenced. The image is sealed again
(`tptp.seal`) and checked. Writing every command of an image again costs 36 to 185 KB, and one
costs a few hundred bytes.

A command is known by where it was read, not by its name, so renaming it (Find model does) or
the device does not lose it. The build compares each command's signal with a digest taken at
import and writes those that differ. A command that cannot be written - no carrier, a protocol
with sender state other than a toggle, a mark over 32767 microseconds - is left as it was and
the build log says so in a `warning:` line (and `changes["warnings"]`). A command that was
**added** has no record to hold it: the group's table has no room and there is nothing that
makes the remote offer it, so it is said not to be written.

A command given as a protocol is written as the frames the protocol renders - nominal timings
(a 9,000 / 4,500 NEC leader) where the image had the measured ones (8,990 / 4,490).

## Checked

Every command of six images (`Update.EZHex`, `H880-Spare-4.EZHex`, `H880-Bedroom.EZHex`,
`H885-LivingRoom.EZHex`, `Update-2.EZHex`, `H785-Config.EZHex`) was read, written again, and
read back with the same carrier, pulses, sections and alternate; each result passes the start
check and every screen program still reads (`tests/test_harmony_880_ir.py`).

## Not known

- **That a remote runs it.** Nothing here has been put on a remote: that the remote follows a
  group entry to a record at the end of the image, and sends what is there, is read from the
  structure, not seen.
- What the 785 and 885 images' records mean for a device the 880 cannot offer; they are read the
  same way and were not tried.
- Whether the remote reads a hold block or a second set differently from what the images
  show; the rare release was read in one image only.
- How to add a command, or a device: that needs the key bindings, the pages and the name table
  as well as the IR (see the Devices page notes in [fonts.md](fonts.md)).
