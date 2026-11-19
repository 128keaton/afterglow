# Harmony 880 devices on the Devices menu

What an 880 image does to list a device, and what Afterglow does to take one off
(`backends/harmony_880/devices.py`). Written from the images; nothing here has been put on a
remote.

## The menu

The Devices menu is a page of cells, a device each. The image keeps **one copy for each state
the remote can be in** - idle, and one for each activity - listing the devices in a different
order (`Update.EZHex` and `H880-Spare-4.EZHex` have six). Each copy is a page of a mode in
section 6; the page record is `u24` binding list, `u24` screen program.

A cell in the screen program is:

| | |
| --- | --- |
| `02 x y u24` | the device's picture, at x = 0 or 64 and y = 24 + 32 for each row |
| `10 n`, `05`/`04 x y ...` | one or two lines of its name (see [fonts.md](fonts.md)) |
| `02 x y u24` | a frame over the picture, at the same place |

Four rows by two columns, eight places. A device has a picture of its own, so the picture
says which device a cell is: it is the same on all six copies.

The page's **binding list** gives each cell's soft key a selection list (`0x7E mode`, ...) that
enters the device's mode. The key is by **place**, not by device: the left column is
`0x85`, `0x86`, `0x87`, `0x88` from the top and the right one `0xAD`, `0xAE`, `0xB0`. The fourth
right-hand key is not known (no image has eight devices), so a page with a device in that place
is left as it is. What says the keys are by place is that, on `Update.EZHex` and
`H880-Spare-4.EZHex`, each device's picture reaches **one IR group on all six copies** through
the key its place gives it (`test_a_device_is_the_same_group_through_its_key_on_every_copy`).
`H885-LivingRoom.EZHex` and `Update-2.EZHex` do not read that way - their pages are rejected
when two copies disagree - and `H785-Config.EZHex` draws its cells differently; they are left
alone.

A device's **IR group** is found from its mode: the one group its binding lists send to
(`0x7D`, through the lists they run with `0x7F`).

## Taking a device off

`devices.remove(payload, groups)` takes the devices whose IR group is in `groups` off every
copy of the menu: the cells after it move up one place, each with its picture, its frame, its
name (a left-hand name from x = 3, a right-hand one ending at x = 125 as
[fonts.md](fonts.md) says) and its key. Each moved line says its own font, because a line does
not always choose it: the next cell in the same face draws in what the one before left. Then
the page's program and binding list are **written again at the end of the image**, the page
record is pointed at them, and nothing that is there moves. The image is sealed and checked.

A copy that does not read, or in which a device cannot move, is **left as it was and warned
of**. Nothing is done in part.

## What it does not do

It takes the device **off the menu** and nothing else. Its infrared group, its modes and the
activities that switch it on stay in the image, as they were, so every activity does what it
did, and the device is no longer offered under Devices. Taking the rest out is not done:
the group table has no spare slot to close, an activity names devices through state
variables and action lists, and each of those would have to be found and changed together
with the Devices menu or the remote would send to nothing.

Adding a device is not done either. It needs an IR group (a record of [ir.md](ir.md) for each
command), a mode with its pages and binding lists, a selection list, a cell on every copy of the
menu and a place for each of its commands on the keys - none of which an image made without it
holds a template for, except by copying another device's, which has not been tried.

## In Afterglow

An import notes the group each device was read from (`_group`) and the groups the image has
(`settings["h880_groups"]`). A build takes off the menu every group whose device is no longer in
the project, after the renames of [fonts.md](fonts.md) and the commands of [ir.md](ir.md). A
device added in the project is warned of and not written.

## Not known

- That the remote follows a page record to a program at the end of the image. The same step
  as [ir.md](ir.md)'s and [fonts.md](fonts.md)'s, by the same argument; none of them has been
  tried.
- The fourth right-hand soft key, and how an image with more than seven devices lays out
  its pages (the `H885-LivingRoom.EZHex` copies list seven or eight and a counter).
- What the remote does for an activity that switches on a device that is no longer listed.
