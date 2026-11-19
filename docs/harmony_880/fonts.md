# Harmony 880 and 885 fonts, screen programs and text

How the 880 stores the words on its screens: a font table of glyphs, screen programs that
draw glyph codes, and a reading of those codes that each image needs for itself. It sits on
the container in [tptp.md](tptp.md). Measured on the sixteen intact images listed there;
`tools/harmony_880_glyph_shapes.py` reproduces the numbers below.

Most of this was written down first by others - the glyph format and the screen opcodes in
[@dannybloe](https://github.com/dannybloe)'s harmony-explorations (`packages/config/src/
screens/`), the arch-8 screen walker and its samples in
[harmony-decompiler](https://github.com/trelowney/harmony-decompiler) - and what is here is
what held when it was read against the images, including three places where it did not.

## The font table (slot 7)

```
u16  count
u24  set[count]          absolute pointer to each font set
```

A font set is `u8` height, `u8` first glyph code (1 in every image), `u8` count and a `u24`
absolute pointer for each code, zero where the set draws nothing for it. Heights are 14, 15,
13, 8 and 11; some images have 8 sets and others 14.

A glyph is a width, runs, and a zero:

| Byte | Meaning |
| --- | --- |
| first | the glyph's width in pixels |
| `0x80 + n` | skip `n` pixels: transparent |
| `n` (1 to 127) | `n` literal pixels follow, two bytes each, RGB565, high byte first |
| `0x00` | end |

A row is finished when it holds exactly `width` pixels, and every row but the last is
followed by a skip of nothing, `0x80`. A row's runs alternate between skips and literals,
and no glyph is wider than 15 pixels, so the 127-pixel limit of a run is never reached in
these images. All 5,193 glyphs of the sixteen images decode, and
`font.encode_glyph` writes every one back as the bytes it was read from.

## Screen programs

A page of the remote is a program in the low region of the image, before slot 0. Text is
drawn with:

| Opcode | Bytes after it | Meaning |
| ---: | --- | --- |
| `10` | `n` | select font set `n` |
| `05` | `x y`, codes, `00` | draw the codes at (x, y) |
| `04` | `x y`, `u24` | draw the codes at that address (a string stored once) |

The other opcodes (fills, pictures, jumps, switches) are walked, not interpreted, by the
vendored decompiler's screen reader (`parse_arch9_screen_program`, which is not particular to
arch 9). A glyph code is an index into the font set in force, not a character.

**Where the programs start is slot 6, not slot 11.** Slot 6 is a `u24` count and a `u24`
pointer to each mode; a mode has a `u16` page count at +4 and a `u24` pointer to each page from
+6; a page is a binding-list pointer and then the program's. In all sixteen images every
program reached this way reads, with no string left unterminated. Slot 11 begins with a `u16`
count and a `u24` for each entry like a table of programs, as the arch-9 reader takes it, but
on this architecture its entries start with opcodes 2 to 8, and a handful of some 1,500 entries
read as screens (5 in the Bedroom image, 2 in one of the user's).

## Reading a glyph code

**Every image numbers its glyphs in its own order.** The Bedroom sample's capital P is code
16 and the Living Room's is code 27; the same letter is a different code in every image, and
a table of codes carried from one to another reads the wrong letters. What is the same
everywhere is the shape a glyph draws, so `glyphs.code_map` reads an image by decoding each
glyph of its font table and looking its shape up in `glyph-shapes.json`: the height and a
digest of the mask - which pixels are drawn, never their colours and never Logitech's pixels -
and the character.

**A font set is read by itself.** Most sets - the text faces - share their image's numbering,
and one code is the same character in every one of them. The small sets do not: the 8-pixel
set of the Bedroom image draws icon-like shapes under codes that mean letters in the others.
A table filled in by carrying a code from one set to another puts a wrong character on those
shapes (it happened while this table was being made, and the rule below is the result), so a set is filled in from the image's numbering only where what the table already
reads in that set agrees with it for nine in ten of its glyphs, judged on at least six.

The table starts from two readings people made off real remotes
(`tools/harmony_880_glyph_seeds.json`, from harmony-decompiler's
`verify_arch8_human_oracles.py`) and from fifty-odd shapes named one at a time from the
words around them (`--unresolved` prints each with those words). With that:

- the user's two own images read 98.3 and 98.5 percent of the glyphs their screens draw, and
  the whole set of sixteen between 88 and 99 percent;
- what is not read is the 8-pixel counter digits, a few arrows, and the ellipsis;
- some faces draw a capital I and a lower-case l alike, and the table knows that shape only as
  an l, so `Input` reads as `lnput`; a shape that two readings give different characters is
  kept as look-alikes and, where the glyphs of the same code in the other sets of the image do
  not settle which, left unread.

### Where the published reading was wrong

The Bedroom sample's published map (harmony-decompiler's oracle, from the owner's screen
inventory) gives `5` as code 9 and `6` as code 8. The pixels of those two glyphs draw a 6 and a
5, and the Living Room map, whose digits agree with its pixels, has them the other way. The two
devices the Bedroom page draws are `32AS500` and `32AS600`, and which is which is read from the
glyphs, not from the map. The seeds leave both digits out of the Bedroom entry.

## The Devices page

A page of cells: the title `Devices` at (3, 10), and for each device a picture and a label of up
to two lines, in font 7 (plain) or font 8 (on a box), at y = 29 and 42 for the first row, 61 and
74 for the second, and further down for more. **A label in a left-hand cell starts at x = 3; a
label in a right-hand cell is right-aligned and ends at x = 125**, so it starts at 125 minus the
width of its glyphs. Over the sixteen images that holds for 876 labels on 110 copies of the page
and for no label that is not a counter: the `1 OF 2` at the foot is centred.

The image keeps **several copies of the page**, one for each order the devices can be shown in
(the Spare-4 image has six), and the remote shows the one that fits its state. The copies share
their strings: each string is drawn inline by one instruction and referenced (`04 x y u24`) by the
rest, so a device that is at the left of one copy and the right of another is drawn from one
string at two different x.

Two photographs of an 880 show this. The remote running `Update.EZHex` draws TV at the left of the
first row, the set-top box at the right as `Digital Set` over `Top Box`, and in the second row
`(DVD,` over `CD, Radio)` at the left and `(With` over `DVD)` at the right: it draws no words
`Mini System` or `Game Console`, because the page does not. The remote running
`H880-Spare-4.EZHex` shows TV at the left and PVR at the right of the first row, `Mini System` at
the left and `Game Console` at the right of the second: the second of its six copies, not the
first in the file.

## Changing text

`labels.rename` finds every string that reads exactly as the old text and changes it where it is
drawn. A string is drawn by one inline instruction and any number of references, so:

- each reference is pointed at a copy of the new text added before the trailer, exactly as long as
  it is, and, where it was right-aligned, given the x that keeps it ending at 125;
- the inline string is written over where it stands when the new text fits, padded with spaces
  after it at the left edge and before it (with a new x) at the right edge;
- when it does not fit, its instruction `05 x y codes 00` (four bytes more than its codes) becomes
  a reference `04 x y u24` (six bytes), and what is left of its room is made harmless: nothing for
  two characters, `10 font` - select the font already in force - for four, a jump over it for six or
  more. A string of one, three or five characters cannot be grown;
- a string that cannot be done wholly (a glyph the font lacks, a text too wide for the right edge,
  bytes shared with another string, too short to grow) is **not done in part**: it is left as it is
  and the note says why.

Nothing after the old trailer moves, the image is sealed again, and every program of the result
still reads. Nothing here has been put on a screen.

An earlier build redrew the first copy of the Devices page from a program of text alone, which lost
its pictures and left the other copies as they were; it is gone.

## Not known

- What the 8-pixel faces' numbering is, and what the counters draw.
- Which characters the unread shapes are: arrows, an ellipsis, a bullet.
- Whether a letter outside the readings (none was seen) would be read: a shape not in the
  table is left unread.
- Whether the remote draws a renamed label as intended, in particular a reference to text
  added after the old end, and an inline instruction turned into a reference: nothing here has
  been put on a screen.
