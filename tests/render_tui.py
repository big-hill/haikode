"""
Screen renderer: run a program on a pty and reconstruct what a terminal shows.

This is the measuring instrument for TUI work, so it is written to be boring
and exact. An earlier crude version understood only a handful of escape
sequences, so ncurses redraws looked like an empty screen and unknown sequences
leaked their letters into the grid; every design decision below exists to make
those two failure modes impossible:

  * the parser is a full ECMA-48 state machine, so ANY sequence -- known or not
    -- is consumed to its final byte instead of falling through as text;
  * every printable character occupies exactly one cell, so row()/find()
    column numbers are literal grid coordinates with nothing to interpret;
  * SGR is parsed and dropped: colour is not what this instrument measures.

Nothing here imports haikode, and the CLI at the bottom uses only the stdlib,
because the usual way to look at a screen is to ssh to the Haiku box and run
this file directly from whatever directory happens to be current.
"""

import argparse
import codecs
import os
import select
import signal
import struct
import sys
import time
import unicodedata
from typing import Dict, List, Optional, Sequence, Tuple

try:  # pragma: no cover - POSIX only; the emulator itself is portable
    import fcntl
    import pty
    import termios
except ImportError:  # pragma: no cover
    fcntl = None
    pty = None
    termios = None

BLANK = " "

# Read size for the pty master. Bigger than any single ncurses refresh so a
# screen usually arrives in one piece.
CHUNK = 65536


class Screen:
    """A VT100/ECMA-48 screen: character grid, cursor, scroll region.

    Coordinates exposed by this class are 0-based (escape sequences are
    1-based; the conversion happens inside). Character attributes are parsed
    and discarded on purpose -- what the TUI review needs to know is which
    glyph sits in which cell, and pretending to model colour would only add
    ways to be wrong.
    """

    def __init__(self, rows: int = 24, cols: int = 80) -> None:
        self.rows = max(1, int(rows))
        self.cols = max(1, int(cols))
        # Incremental so a UTF-8 character split across two os.read() calls is
        # rejoined instead of turning into two replacement characters.
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        # Replies to device queries (DSR/DA); run_tui writes them back so a
        # program that blocks waiting for an answer does not stall.
        self.replies = []  # type: List[bytes]
        self.title = ""
        # Filled in by run_tui; predeclared so callers can always read them.
        self.pid = None  # type: Optional[int]
        self.exit_status = None  # type: Optional[int]
        self.elapsed = 0.0
        self.timed_out = False
        self.reset()

    # -- state ------------------------------------------------------------

    def reset(self) -> None:
        """Full reset (RIS). Leaves the UTF-8 decoder alone: it is mid-stream."""
        self.grid = self._blank_grid()
        self.cursor_row = 0
        self.cursor_col = 0
        self.top = 0
        self.bottom = self.rows - 1
        self.autowrap = True
        self.origin_mode = False
        self.insert_mode = False
        self.newline_mode = False
        self.cursor_visible = True
        self._wrap_pending = False
        self._saved = None  # type: Optional[Tuple[int, int, bool]]
        self._alt = None  # type: Optional[Tuple[List[List[str]], int, int]]
        self._alt_exit = None  # type: Optional[List[List[str]]]
        self._tabs = set(range(8, self.cols, 8))
        self._last_graphic = BLANK
        self._state = "ground"
        self._pbuf = ""
        self._interm = ""
        self._priv = ""
        self._ostr = ""

    def _blank_grid(self) -> List[List[str]]:
        return [[BLANK] * self.cols for _ in range(self.rows)]

    @property
    def cursor(self) -> Tuple[int, int]:
        """Cursor as (row, col), 0-based."""
        return (self.cursor_row, self.cursor_col)

    @property
    def alt_screen(self) -> bool:
        return self._alt is not None

    def take_replies(self) -> List[bytes]:
        """Hand over queued device-query answers and forget them."""
        out = self.replies
        self.replies = []
        return out

    # -- input ------------------------------------------------------------

    def feed(self, data) -> "Screen":
        """Consume bytes (decoded incrementally) or already-decoded text."""
        if isinstance(data, (bytes, bytearray, memoryview)):
            text = self._decoder.decode(bytes(data))
        else:
            text = data
        for ch in text:
            self._consume(ch)
        return self

    def _consume(self, ch: str) -> None:
        state = self._state
        if state == "ground":
            self._ground(ch)
        elif state == "esc":
            self._esc(ch)
        elif state == "esc_interm":
            self._esc_interm(ch)
        elif state == "csi":
            self._csi(ch)
        elif state == "osc":
            self._osc(ch)
        else:  # "string": DCS/SOS/PM/APC payloads are discarded
            self._string(ch)

    # -- ground state -----------------------------------------------------

    def _ground(self, ch: str) -> None:
        code = ord(ch)
        if code == 0x1B:
            self._enter_esc()
        elif code == 0x9B:  # 8-bit CSI
            self._enter_csi()
        elif code == 0x9D:  # 8-bit OSC
            self._state = "osc"
            self._ostr = ""
        elif code < 0x20:
            self._execute(code)
        elif code == 0x7F or 0x80 <= code <= 0x9F:
            pass  # DEL and the remaining C1 controls print nothing
        else:
            self._put(ch)

    def _execute(self, code: int) -> None:
        if code == 0x08:  # BS
            self._wrap_pending = False
            self.cursor_col = max(0, self.cursor_col - 1)
        elif code == 0x09:  # HT
            self._wrap_pending = False
            self.cursor_col = self._next_tab(self.cursor_col)
        elif code in (0x0A, 0x0B, 0x0C):  # LF, VT, FF
            self._index()
            if self.newline_mode:
                self.cursor_col = 0
            self._wrap_pending = False
        elif code == 0x0D:  # CR
            self._wrap_pending = False
            self.cursor_col = 0
        # BEL, SO/SI and the rest are deliberately silent.

    def _put(self, ch: str) -> None:
        if self._wrap_pending:
            # Deferred wrap: writing the last column parks the cursor there and
            # only the NEXT character moves to the following line. Getting this
            # wrong shifts whole paragraphs by one line.
            self._wrap_pending = False
            if self.autowrap:
                self.cursor_col = 0
                self._index()
        if self.cursor_col >= self.cols:
            self.cursor_col = self.cols - 1
        line = self.grid[self.cursor_row]
        if self.insert_mode:
            line.insert(self.cursor_col, BLANK)
            del line[self.cols:]
        line[self.cursor_col] = ch
        self._last_graphic = ch
        if self.cursor_col + 1 >= self.cols:
            self._wrap_pending = self.autowrap
        else:
            self.cursor_col += 1

    # -- escape sequences -------------------------------------------------

    def _enter_esc(self) -> None:
        self._state = "esc"
        self._interm = ""

    def _enter_csi(self) -> None:
        self._state = "csi"
        self._pbuf = ""
        self._interm = ""
        self._priv = ""

    def _esc(self, ch: str) -> None:
        code = ord(ch)
        if ch == "[":
            self._enter_csi()
            return
        if ch == "]":
            self._state = "osc"
            self._ostr = ""
            return
        if ch in ("P", "X", "^", "_"):  # DCS, SOS, PM, APC
            self._state = "string"
            return
        if code == 0x1B:  # ESC ESC: restart, do not print
            self._enter_esc()
            return
        if 0x20 <= code <= 0x2F:  # ( ) * + - . / # % and friends
            self._state = "esc_interm"
            self._interm = ch
            return
        self._state = "ground"
        if code < 0x20:
            self._execute(code)
            return
        if ch == "7":  # DECSC
            self._saved = (self.cursor_row, self.cursor_col, self.origin_mode)
        elif ch == "8":  # DECRC
            self._restore_cursor()
        elif ch == "D":  # IND
            self._index()
        elif ch == "E":  # NEL
            self.cursor_col = 0
            self._index()
            self._wrap_pending = False
        elif ch == "M":  # RI
            self._reverse_index()
        elif ch == "H":  # HTS
            self._tabs.add(self.cursor_col)
        elif ch == "c":  # RIS
            self.reset()
        elif ch == "Z":  # DECID
            self.replies.append(b"\x1b[?1;2c")
        # ESC =, ESC >, ESC \ (ST) and unknown finals: consumed, no effect.

    def _esc_interm(self, ch: str) -> None:
        code = ord(ch)
        if 0x20 <= code <= 0x2F:
            self._interm += ch
            return
        # Final byte of e.g. ESC ( B (charset) or ESC # 8 (DECALN): consumed
        # without effect. Charsets matter only for line drawing, which ncurses
        # emits as UTF-8 here.
        self._state = "ground"
        if code < 0x20:
            self._execute(code)

    def _osc(self, ch: str) -> None:
        code = ord(ch)
        if code == 0x07 or code == 0x9C:  # BEL or 8-bit ST
            self._end_osc()
        elif code == 0x1B:  # ESC: ends the string; ESC \ lands on ST below
            self._end_osc()
            self._enter_esc()
        else:
            self._ostr += ch

    def _end_osc(self) -> None:
        self._state = "ground"
        head, sep, rest = self._ostr.partition(";")
        if sep and head in ("0", "2"):
            self.title = rest
        self._ostr = ""

    def _string(self, ch: str) -> None:
        code = ord(ch)
        if code == 0x9C:
            self._state = "ground"
        elif code == 0x1B:
            self._enter_esc()

    # -- CSI --------------------------------------------------------------

    def _csi(self, ch: str) -> None:
        code = ord(ch)
        if code == 0x1B:
            self._enter_esc()
            return
        if code < 0x20:
            self._execute(code)
            return
        if code == 0x7F:
            return
        if 0x30 <= code <= 0x3F:  # digits, ; : and the private markers < = > ?
            if 0x3C <= code <= 0x3F and not self._pbuf:
                self._priv = ch
            else:
                self._pbuf += ch
            return
        if 0x20 <= code <= 0x2F:
            self._interm += ch
            return
        if 0x40 <= code <= 0x7E:
            self._state = "ground"
            self._dispatch(ch)
            return
        self._state = "ground"  # not reachable for 7-bit input

    def _numbers(self) -> List[Optional[int]]:
        """Parse the parameter string; an omitted parameter becomes None."""
        out = []  # type: List[Optional[int]]
        for part in self._pbuf.split(";"):
            part = part.split(":", 1)[0].strip()
            if not part.isdigit():
                out.append(None)
            else:
                out.append(int(part))
        return out

    def _arg(self, params: Sequence[Optional[int]], index: int,
             default: int = 1, minimum: int = 1) -> int:
        value = params[index] if index < len(params) else None
        if value is None:
            value = default
        return max(minimum, value)

    def _dispatch(self, final: str) -> None:
        if self._interm:
            # Sequences with intermediates (DECSCUSR, DECSCA, XTerm's $-forms)
            # carry no layout meaning for us -- consumed, never guessed at.
            return
        params = self._numbers()
        if self._priv:
            if final in ("h", "l"):
                self._set_modes(params, final == "h", private=True)
            elif final == "c" and self._priv == ">":
                self.replies.append(b"\x1b[>0;95;0c")
            return

        if final in ("H", "f"):  # CUP / HVP
            self._goto(self._arg(params, 0) - 1, self._arg(params, 1) - 1)
        elif final == "A":  # CUU
            self._move_vertical(-self._arg(params, 0))
        elif final in ("B", "e"):  # CUD / VPR
            self._move_vertical(self._arg(params, 0))
        elif final in ("C", "a"):  # CUF / HPR
            self._wrap_pending = False
            self.cursor_col = min(self.cols - 1, self.cursor_col + self._arg(params, 0))
        elif final == "D":  # CUB
            self._wrap_pending = False
            self.cursor_col = max(0, self.cursor_col - self._arg(params, 0))
        elif final == "E":  # CNL
            self._move_vertical(self._arg(params, 0))
            self.cursor_col = 0
        elif final == "F":  # CPL
            self._move_vertical(-self._arg(params, 0))
            self.cursor_col = 0
        elif final in ("G", "`"):  # CHA / HPA
            self._wrap_pending = False
            self.cursor_col = self._clamp_col(self._arg(params, 0) - 1)
        elif final == "d":  # VPA
            self._goto(self._arg(params, 0) - 1, self.cursor_col, keep_col=True)
        elif final == "I":  # CHT
            self._wrap_pending = False
            for _ in range(self._arg(params, 0)):
                self.cursor_col = self._next_tab(self.cursor_col)
        elif final == "Z":  # CBT
            self._wrap_pending = False
            for _ in range(self._arg(params, 0)):
                self.cursor_col = self._prev_tab(self.cursor_col)
        elif final == "J":  # ED
            self._erase_display(self._arg(params, 0, default=0, minimum=0))
        elif final == "K":  # EL
            self._erase_line(self._arg(params, 0, default=0, minimum=0))
        elif final == "L":  # IL
            self._insert_lines(self._arg(params, 0))
        elif final == "M":  # DL
            self._delete_lines(self._arg(params, 0))
        elif final == "@":  # ICH
            self._insert_chars(self._arg(params, 0))
        elif final == "P":  # DCH
            self._delete_chars(self._arg(params, 0))
        elif final == "X":  # ECH
            self._erase_chars(self._arg(params, 0))
        elif final == "S":  # SU
            self._wrap_pending = False
            self._scroll_up(self._arg(params, 0))
        elif final == "T":
            # One parameter is SD; the five-parameter form is mouse tracking.
            if len(params) <= 1:
                self._wrap_pending = False
                self._scroll_down(self._arg(params, 0))
        elif final == "b":  # REP
            for _ in range(self._arg(params, 0)):
                self._put(self._last_graphic)
        elif final == "g":  # TBC
            if self._arg(params, 0, default=0, minimum=0) == 3:
                self._tabs.clear()
            else:
                self._tabs.discard(self.cursor_col)
        elif final in ("h", "l"):
            self._set_modes(params, final == "h", private=False)
        elif final == "r":  # DECSTBM
            self._set_scroll_region(params)
        elif final == "s":  # save cursor (ANSI.SYS flavour)
            self._saved = (self.cursor_row, self.cursor_col, self.origin_mode)
        elif final == "u":  # restore cursor
            self._restore_cursor()
        elif final == "n":  # DSR
            self._device_status(self._arg(params, 0, default=0, minimum=0))
        elif final == "c":  # DA1
            self.replies.append(b"\x1b[?1;2c")
        # SGR ("m") is parsed above and intentionally has no effect: this
        # instrument measures layout, not colour. Everything else -- DECSCA,
        # window ops, unknown finals -- is consumed silently.

    def _device_status(self, what: int) -> None:
        if what == 5:
            self.replies.append(b"\x1b[0n")
        elif what == 6:
            row = self.cursor_row - self.top if self.origin_mode else self.cursor_row
            self.replies.append(
                ("\x1b[%d;%dR" % (row + 1, self.cursor_col + 1)).encode("ascii"))

    def _set_modes(self, params: Sequence[Optional[int]], on: bool,
                   private: bool) -> None:
        for value in params:
            if value is None:
                continue
            if private:
                if value == 6:  # DECOM
                    self.origin_mode = on
                    self._goto(0, 0)
                elif value == 7:  # DECAWM
                    self.autowrap = on
                    self._wrap_pending = False
                elif value == 25:  # DECTCEM
                    self.cursor_visible = on
                elif value in (47, 1047, 1049):
                    self._switch_alt(on)
            else:
                if value == 4:  # IRM
                    self.insert_mode = on
                elif value == 20:  # LNM
                    self.newline_mode = on

    def _switch_alt(self, on: bool) -> None:
        """Model smcup/rmcup with two buffers so shell scrollback never bleeds
        into the captured full-screen image."""
        if on:
            if self._alt is None:
                self._alt = (self.grid, self.cursor_row, self.cursor_col)
                self.grid = self._blank_grid()
                self.cursor_row = 0
                self.cursor_col = 0
                self._wrap_pending = False
        elif self._alt is not None:
            # Keep what the full-screen program had drawn: rmcup blanks the
            # capture, and a blank capture is precisely the answer this tool
            # exists to stop giving.
            self._alt_exit = self.grid
            self.grid, self.cursor_row, self.cursor_col = self._alt
            self._alt = None
            self._wrap_pending = False

    def _set_scroll_region(self, params: Sequence[Optional[int]]) -> None:
        top = self._arg(params, 0, default=1) - 1
        bottom = min(self._arg(params, 1, default=self.rows) - 1, self.rows - 1)
        if not 0 <= top < bottom:
            return  # a region of fewer than two lines is ignored outright
        self.top = top
        self.bottom = bottom
        self._wrap_pending = False
        self._goto(0, 0)

    # -- cursor helpers ---------------------------------------------------

    def _clamp_row(self, row: int) -> int:
        return max(0, min(self.rows - 1, row))

    def _clamp_col(self, col: int) -> int:
        return max(0, min(self.cols - 1, col))

    def _goto(self, row: int, col: int, keep_col: bool = False) -> None:
        if self.origin_mode:
            row = max(self.top, min(self.bottom, self.top + row))
        self.cursor_row = self._clamp_row(row)
        if not keep_col:
            self.cursor_col = self._clamp_col(col)
        self._wrap_pending = False

    def _move_vertical(self, delta: int) -> None:
        """CUU/CUD stop at the scroll region edge instead of scrolling."""
        self._wrap_pending = False
        row = self.cursor_row + delta
        if delta < 0 and self.cursor_row >= self.top:
            row = max(row, self.top)
        elif delta > 0 and self.cursor_row <= self.bottom:
            row = min(row, self.bottom)
        self.cursor_row = self._clamp_row(row)

    def _restore_cursor(self) -> None:
        if self._saved is None:
            self._goto(0, 0)
            return
        row, col, origin = self._saved
        self.origin_mode = origin
        self.cursor_row = self._clamp_row(row)
        self.cursor_col = self._clamp_col(col)
        self._wrap_pending = False

    def _next_tab(self, col: int) -> int:
        stops = sorted(t for t in self._tabs if t > col)
        return self._clamp_col(stops[0] if stops else self.cols - 1)

    def _prev_tab(self, col: int) -> int:
        stops = sorted(t for t in self._tabs if t < col)
        return self._clamp_col(stops[-1] if stops else 0)

    def _index(self) -> None:
        """LF: move down, scrolling when sitting on the scroll region bottom."""
        self._wrap_pending = False
        if self.cursor_row == self.bottom:
            self._scroll_up(1)
        elif self.cursor_row < self.rows - 1:
            self.cursor_row += 1

    def _reverse_index(self) -> None:
        if self.cursor_row == self.top:
            self._scroll_down(1)
        elif self.cursor_row > 0:
            self.cursor_row -= 1
        self._wrap_pending = False

    # -- editing ----------------------------------------------------------

    def _blank_lines(self, count: int) -> List[List[str]]:
        return [[BLANK] * self.cols for _ in range(count)]

    def _replace(self, first: int, last: int, block: List[List[str]]) -> None:
        """Write `block` back over rows first..last inclusive."""
        self.grid[first:last + 1] = block

    def _scroll_up(self, count: int) -> None:
        count = min(count, self.bottom - self.top + 1)
        block = self.grid[self.top:self.bottom + 1]
        self._replace(self.top, self.bottom,
                      block[count:] + self._blank_lines(count))

    def _scroll_down(self, count: int) -> None:
        count = min(count, self.bottom - self.top + 1)
        block = self.grid[self.top:self.bottom + 1]
        keep = block[:len(block) - count]
        self._replace(self.top, self.bottom, self._blank_lines(count) + keep)

    def _insert_lines(self, count: int) -> None:
        # Only meaningful inside the scroll region; the column is left alone,
        # matching xterm and the Linux console (ncurses relies on that).
        if not self.top <= self.cursor_row <= self.bottom:
            return
        self._wrap_pending = False
        count = min(count, self.bottom - self.cursor_row + 1)
        block = self.grid[self.cursor_row:self.bottom + 1]
        keep = block[:len(block) - count]
        self._replace(self.cursor_row, self.bottom, self._blank_lines(count) + keep)

    def _delete_lines(self, count: int) -> None:
        if not self.top <= self.cursor_row <= self.bottom:
            return
        self._wrap_pending = False
        count = min(count, self.bottom - self.cursor_row + 1)
        block = self.grid[self.cursor_row:self.bottom + 1]
        self._replace(self.cursor_row, self.bottom,
                      block[count:] + self._blank_lines(count))

    def _insert_chars(self, count: int) -> None:
        self._wrap_pending = False
        line = self.grid[self.cursor_row]
        count = min(count, self.cols - self.cursor_col)
        for _ in range(count):
            line.insert(self.cursor_col, BLANK)
        del line[self.cols:]

    def _delete_chars(self, count: int) -> None:
        self._wrap_pending = False
        line = self.grid[self.cursor_row]
        count = min(count, self.cols - self.cursor_col)
        del line[self.cursor_col:self.cursor_col + count]
        line.extend([BLANK] * count)

    def _erase_chars(self, count: int) -> None:
        self._wrap_pending = False
        line = self.grid[self.cursor_row]
        for col in range(self.cursor_col, min(self.cols, self.cursor_col + count)):
            line[col] = BLANK

    def _erase_display(self, mode: int) -> None:
        self._wrap_pending = False
        if mode == 0:
            self._erase_line(0)
            for row in range(self.cursor_row + 1, self.rows):
                self.grid[row] = [BLANK] * self.cols
        elif mode == 1:
            self._erase_line(1)
            for row in range(0, self.cursor_row):
                self.grid[row] = [BLANK] * self.cols
        else:  # 2 and 3 (3 also drops scrollback, which we do not keep)
            self.grid = self._blank_grid()

    def _erase_line(self, mode: int) -> None:
        self._wrap_pending = False
        line = self.grid[self.cursor_row]
        if mode == 0:
            for col in range(self.cursor_col, self.cols):
                line[col] = BLANK
        elif mode == 1:
            for col in range(0, min(self.cursor_col + 1, self.cols)):
                line[col] = BLANK
        else:
            self.grid[self.cursor_row] = [BLANK] * self.cols

    # -- output -----------------------------------------------------------

    def row_text(self, index: int) -> str:
        """Row `index` at full width, trailing blanks included."""
        return "".join(self.grid[index])

    def row(self, index: int, strip: bool = True) -> str:
        text = self.row_text(index)
        return text.rstrip(BLANK) if strip else text

    def text(self) -> str:
        return "\n".join(self.row(i) for i in range(self.rows))

    def lines(self) -> List[str]:
        return [self.row(i) for i in range(self.rows)]

    def find(self, needle: str) -> Optional[Tuple[int, int]]:
        """First (row, col) where `needle` appears, scanning top to bottom."""
        for index in range(self.rows):
            col = self.row_text(index).find(needle)
            if col >= 0:
                return (index, col)
        return None

    def find_all(self, needle: str) -> List[Tuple[int, int]]:
        hits = []
        for index in range(self.rows):
            line = self.row_text(index)
            start = line.find(needle)
            while start >= 0:
                hits.append((index, start))
                start = line.find(needle, start + 1)
        return hits

    def nonblank_rows(self) -> int:
        return sum(1 for i in range(self.rows) if self.row(i))

    def wide_chars(self) -> List[Tuple[int, int, str]]:
        """Cells holding an East Asian wide character, as (row, col, char).

        One character is one cell here, which is exactly right for everything
        haikode draws -- box drawing, arrows, the dot and the em dash all
        measure one column in ncurses, verified against it. Only genuine
        double-width characters (CJK) would make a real terminal disagree, so
        they are reported rather than quietly shifting every column to their
        right. An empty list means the reconstruction is column-exact.
        """
        found = []
        for row in range(self.rows):
            for col, ch in enumerate(self.grid[row]):
                # U+1100 is where the first wide range starts; skipping below
                # it keeps this cheap enough to call on every capture.
                if ch >= "ᄀ" and unicodedata.east_asian_width(ch) in ("W", "F"):
                    found.append((row, col, ch))
        return found

    def frame(self) -> str:
        """The grid inside a +---+ border, for eyeballing a capture."""
        return self._frame_of(self.grid)

    def _frame_of(self, grid: List[List[str]]) -> str:
        edge = "+" + "-" * self.cols + "+"
        out = [edge]
        for line in grid:
            out.append("|" + "".join(line) + "|")
        out.append(edge)
        return "\n".join(out)

    def alt_exit_frame(self) -> Optional[str]:
        """The alternate screen as it looked when the program left it.

        A full-screen program that quits emits rmcup, which correctly restores
        the shell's screen -- so a capture taken after it exited is blank
        through no fault of the drawing code. This keeps the last real image
        around so "it rendered nothing" is never reported by accident.
        """
        if self._alt_exit is None:
            return None
        return self._frame_of(self._alt_exit)

    def summary(self) -> str:
        parts = [
            "size=%dx%d" % (self.rows, self.cols),
            "nonblank_rows=%d" % self.nonblank_rows(),
            "cursor=%d,%d" % (self.cursor_row, self.cursor_col),
            "alt_screen=%s" % ("yes" if self.alt_screen else "no"),
        ]
        if self.title:
            parts.append("title=%r" % self.title)
        if self.exit_status is not None:
            parts.append("exit=%s" % _describe_status(self.exit_status))
        if self.elapsed:
            parts.append("elapsed=%.1fs" % self.elapsed)
        if self.timed_out:
            parts.append("TIMED-OUT")
        wide = self.wide_chars()
        if wide:
            parts.append("WARNING=%d double-width chars, columns to their "
                         "right read low by one each" % len(wide))
        return " ".join(parts)

    def __str__(self) -> str:
        return self.text()


# --------------------------------------------------------------------------
# running a program on a pty
# --------------------------------------------------------------------------


def _describe_status(status: Optional[int]) -> str:
    if status is None:
        return "unknown"
    if os.WIFSIGNALED(status):
        return "signal %d" % os.WTERMSIG(status)
    if os.WIFEXITED(status):
        return str(os.WEXITSTATUS(status))
    return str(status)


def _write_all(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data):]


def _reap(pid: int, grace: float = 2.0) -> Optional[int]:
    """Never leave a pty child behind: a stray ncurses process wedges a tty."""
    for sig in (None, signal.SIGHUP, signal.SIGKILL):
        if sig is not None:
            try:
                os.kill(pid, sig)
            except OSError:
                return None
        deadline = time.monotonic() + grace
        while True:
            try:
                done, status = os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                return None
            if done == pid:
                return status
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
    return None


def run_tui(argv: Sequence[str], rows: int = 32, cols: int = 110,
            keys: Optional[Sequence[Tuple[float, object]]] = None,
            settle: float = 1.0, timeout: float = 30.0,
            env: Optional[Dict[str, str]] = None) -> Screen:
    """Run `argv` on a pty of the given size and return the rendered Screen.

    `keys` is a sequence of (delay, data) pairs; delay is seconds measured from
    the start of the run (absolute, not relative to the previous key) and data
    is bytes or str. The call returns once the program has produced output and
    then been quiet for `settle` seconds with no keys left to send, or when it
    exits, or when `timeout` is reached -- it never raises on timeout, it just
    returns what was on screen (screen.timed_out says which happened). A
    program that never writes anything therefore costs the full timeout, which
    is the honest answer rather than a fast blank one.

    To photograph a full-screen program, do NOT send it a quit key: quitting
    runs rmcup and the capture shows the restored shell screen instead. Let
    the settle timer fire while it is still drawing; the hangup on close is
    what shuts it down. Screen.alt_exit_frame() covers the case anyway.
    """
    if pty is None or termios is None or fcntl is None:  # pragma: no cover
        raise RuntimeError("run_tui needs a POSIX platform with pty support")
    argv = [str(a) for a in argv]
    if not argv:
        raise ValueError("argv must not be empty")

    child_env = dict(os.environ)
    if env:
        child_env.update((str(k), str(v)) for k, v in env.items())
    child_env.setdefault("TERM", "xterm")
    # ncurses trusts LINES/COLUMNS over the kernel window size, so an inherited
    # pair from the parent terminal would silently override TIOCSWINSZ below.
    child_env.pop("LINES", None)
    child_env.pop("COLUMNS", None)

    screen = Screen(rows, cols)
    winsize = struct.pack("HHHH", screen.rows, screen.cols, 0, 0)

    pid, fd = pty.fork()
    if pid == 0:  # child: nothing here may raise back into the caller
        try:
            fcntl.ioctl(0, termios.TIOCSWINSZ, winsize)
        except Exception:
            pass
        try:
            os.execvpe(argv[0], argv, child_env)
        except BaseException:
            pass
        os._exit(127)

    schedule = []  # type: List[Tuple[float, bytes]]
    for delay, data in (keys or []):
        if not isinstance(data, (bytes, bytearray)):
            data = str(data).encode("utf-8")
        schedule.append((float(delay), bytes(data)))
    schedule.sort(key=lambda item: item[0])

    start = time.monotonic()
    deadline = start + float(timeout)
    quiet_since = start
    saw_output = False
    timed_out = False
    status = None
    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                timed_out = True
                break
            wait = 0.05
            if schedule:
                wait = min(wait, max(0.0, start + schedule[0][0] - now))
            try:
                readable = select.select([fd], [], [], wait)[0]
            except (OSError, ValueError):
                break
            if readable:
                try:
                    data = os.read(fd, CHUNK)
                except OSError:
                    break  # EIO: the child closed the slave side
                if not data:
                    break
                screen.feed(data)
                quiet_since = time.monotonic()
                saw_output = True
                pending = screen.take_replies()
                if pending:
                    try:
                        _write_all(fd, b"".join(pending))
                    except OSError:
                        pass
            now = time.monotonic()
            while schedule and now >= start + schedule[0][0]:
                try:
                    _write_all(fd, schedule.pop(0)[1])
                except OSError:
                    del schedule[:]
                    break
                # Restart the quiet timer: the program deserves `settle`
                # seconds to redraw after the last keystroke.
                quiet_since = time.monotonic()
            # `saw_output` guards the one wrong answer this tool must never
            # give: a program that is merely slow to start would otherwise let
            # the settle timer expire and be reported as a blank screen.
            if (saw_output and not schedule
                    and (time.monotonic() - quiet_since) >= settle):
                break
    finally:
        # Close first: the hangup is what makes a curses child exit cleanly.
        try:
            os.close(fd)
        except OSError:
            pass
        status = _reap(pid)

    screen.pid = pid
    screen.exit_status = status
    screen.elapsed = time.monotonic() - start
    screen.timed_out = timed_out
    return screen


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


_ESCAPES = {
    "a": "\a", "b": "\b", "e": "\x1b", "f": "\f", "n": "\n",
    "r": "\r", "t": "\t", "v": "\v", "0": "\x00", "\\": "\\", ":": ":",
}


def unescape(text: str) -> str:
    """Decode the backslash escapes accepted by --keys (\\r, \\e, \\x1b, ...)."""
    out = []
    index = 0
    while index < len(text):
        ch = text[index]
        if ch != "\\" or index + 1 >= len(text):
            out.append(ch)
            index += 1
            continue
        nxt = text[index + 1]
        if nxt == "x" and index + 4 <= len(text):
            try:
                out.append(chr(int(text[index + 2:index + 4], 16)))
                index += 4
                continue
            except ValueError:
                pass
        out.append(_ESCAPES.get(nxt, nxt))
        index += 2
    return "".join(out)


def parse_key(spec: str) -> Tuple[float, bytes]:
    """Turn "1.5:hello\\r" into (1.5, b"hello\\r"); a bare string means t=0."""
    delay = 0.0
    text = spec
    head, sep, rest = spec.partition(":")
    if sep:
        try:
            delay = float(head)
            text = rest
        except ValueError:
            delay, text = 0.0, spec
    return (delay, unescape(text).encode("utf-8"))


def _emit(text: str) -> None:
    """Write without dying on a terminal that cannot encode box drawing."""
    stream = getattr(sys.stdout, "buffer", None)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    if stream is None:  # pragma: no cover - redirected StringIO
        sys.stdout.write(text + "\n")
        return
    stream.write((text + "\n").encode(encoding, "replace"))
    stream.flush()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="render_tui.py",
        description="Run a command on a pty and print what the screen shows.")
    parser.add_argument("--rows", type=int, default=32)
    parser.add_argument("--cols", type=int, default=110)
    parser.add_argument("--keys", action="append", default=[],
                        metavar="DELAY:TEXT",
                        help="keystrokes to send, e.g. --keys '1.0:hello\\r'; "
                             "DELAY is seconds from start, repeatable")
    parser.add_argument("--settle", type=float, default=1.0,
                        help="seconds of silence before capturing (default 1)")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--term", default=None, help="TERM for the child")
    parser.add_argument("--env", action="append", default=[],
                        metavar="NAME=VALUE", help="extra environment, repeatable")
    parser.add_argument("--text", action="store_true",
                        help="print bare rows instead of the bordered grid")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="-- followed by the command to run")
    args = parser.parse_args(argv)

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("no command given; put it after --")

    extra = {}
    for item in args.env:
        name, _, value = item.partition("=")
        if name:
            extra[name] = value
    if args.term:
        extra["TERM"] = args.term

    screen = run_tui(command, rows=args.rows, cols=args.cols,
                     keys=[parse_key(k) for k in args.keys],
                     settle=args.settle, timeout=args.timeout,
                     env=extra or None)
    _emit(screen.text() if args.text else screen.frame())
    _emit(screen.summary())
    leftover = screen.alt_exit_frame()
    if leftover is not None and screen.nonblank_rows() == 0:
        _emit("")
        _emit("The program left its full-screen mode, which blanks the "
              "capture above. Last full-screen image:")
        _emit(leftover)
    return 0


if __name__ == "__main__":
    sys.exit(main())
