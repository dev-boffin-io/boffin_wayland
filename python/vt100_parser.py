"""
vt100_parser.py — a small but real VT100/xterm-subset terminal emulator core.

This module has two halves:

  * Screen        - a rows x cols grid of Cell objects (character + colors +
                    attributes), plus cursor state, scroll region, an
                    alternate screen buffer (used by full-screen apps like
                    nano/vim/htop), and a scrollback buffer for the primary
                    screen. All the actual "what does the terminal look
                    like right now" state lives here, along with fine-
                    grained dirty tracking so the Kivy layer can do
                    selective (per-row) redraws instead of rebuilding the
                    whole screen on every change.

  * VTParser      - a byte-stream state machine. Feed it raw bytes coming
                    off the PTY master fd; it decodes UTF-8 incrementally
                    (safe across chunk boundaries) and turns escape
                    sequences into calls on a Screen instance.

Dirty tracking (consumed by TerminalView.pop_dirty() equivalent):
  - dirty_rows: set of grid row indices whose *content* changed (a
    character was written, a line was erased) - only those rows need their
    canvas instructions rebuilt.
  - dirty_cursor: the cursor moved but no content changed (arrow keys,
    plain cursor positioning) - the renderer only needs to move the cursor
    overlay rectangle, not touch any row.
  - dirty_all: a structural change (resize, full erase, alt-screen toggle,
    scrolling) that can shift or replace many rows at once - the renderer
    rebuilds every visible row.
  - view_dirty: the scrollback viewport offset changed (user scrolled).
  Screen.pop_dirty() atomically reads and clears all four under the lock.

Scrollback:
  - Screen.scrollback is a bounded deque (SCROLLBACK_MAX lines) that
    captures lines scrolled off the top of the *primary* screen only.
    It's intentionally left untouched while the alternate screen buffer is
    active (nano/vim/htop) and reset to "live" whenever alt-screen is
    entered, matching real terminal behavior.
  - Screen.scroll_view(delta) / scroll_to_bottom() move a view offset;
    Screen.get_visible_rows() returns whatever should currently be on
    screen (either the live grid, or a slice of scrollback+grid).

Deliberately supported (covers ls --color, nano, vim, htop reasonably well):
  - SGR (colors: 16-color, 256-color palette, 24-bit truecolor; bold;
    underline; reverse video; reset)
  - Cursor movement: CUU/CUD/CUF/CUB/CUP/HVP/CHA/VPA
  - Erase in display / erase in line (all modes)
  - Scrolling region (DECSTBM) + SU/SD
  - Save/restore cursor (both ESC 7/8 and CSI s/u forms)
  - Alternate screen buffer (?1049h/l, ?47h/l, ?1047h/l) - what lets
    nano/vim/htop redraw a full-screen UI and cleanly restore your shell
    scrollback when they exit
  - Cursor visibility (?25h/l)
  - OSC sequences (window title etc.) are recognized and *skipped* rather
    than printed as garbage

Deliberately NOT supported yet (honest limitations, not silent gaps):
  - True double-width/double-height characters (DECDHL/DECDWL)
  - Mouse reporting, bracketed paste, and most other xterm private modes
  - Per-cell background colors ARE tracked here and (as of the canvas-based
    TerminalView) rendered correctly in main.py - the old markup-Label
    renderer's inability to show them is no longer a limitation.
"""

import codecs
import threading
from collections import deque

# ---------------------------------------------------------------------------
# Color tables
# ---------------------------------------------------------------------------

ANSI_COLORS = [
    (0x00, 0x00, 0x00), (0xCD, 0x00, 0x00), (0x00, 0xCD, 0x00), (0xCD, 0xCD, 0x00),
    (0x00, 0x00, 0xEE), (0xCD, 0x00, 0xCD), (0x00, 0xCD, 0xCD), (0xE5, 0xE5, 0xE5),
]
ANSI_BRIGHT_COLORS = [
    (0x7F, 0x7F, 0x7F), (0xFF, 0x00, 0x00), (0x00, 0xFF, 0x00), (0xFF, 0xFF, 0x00),
    (0x5C, 0x5C, 0xFF), (0xFF, 0x00, 0xFF), (0x00, 0xFF, 0xFF), (0xFF, 0xFF, 0xFF),
]


def palette_256(n: int):
    """Standard xterm 256-color palette lookup -> (r, g, b)."""
    n = max(0, min(255, n))
    if n < 8:
        return ANSI_COLORS[n]
    if n < 16:
        return ANSI_BRIGHT_COLORS[n - 8]
    if n < 232:
        n -= 16
        r, g, b = n // 36, (n % 36) // 6, n % 6
        scale = lambda v: 0 if v == 0 else 55 + v * 40
        return (scale(r), scale(g), scale(b))
    gray = 8 + (n - 232) * 10
    return (gray, gray, gray)


DEFAULT_FG = (0xE0, 0xE0, 0xE0)
DEFAULT_BG = (0x00, 0x00, 0x00)


class Cell:
    __slots__ = ("ch", "fg", "bg", "bold", "underline", "reverse")

    def __init__(self, ch=" ", fg=None, bg=None, bold=False, underline=False, reverse=False):
        self.ch = ch
        self.fg = fg          # (r,g,b) or None = default
        self.bg = bg          # (r,g,b) or None = default
        self.bold = bold
        self.underline = underline
        self.reverse = reverse

    def copy(self):
        return Cell(self.ch, self.fg, self.bg, self.bold, self.underline, self.reverse)


SCROLLBACK_MAX = 2000  # lines of primary-screen history retained


class Screen:
    """Rows x cols character grid + cursor state. Thread-safe: the PTY
    reader thread mutates it while the Kivy main thread reads snapshots.

    Dirty tracking is deliberately fine-grained (see module docstring) so
    the renderer can skip untouched rows. Every mutating method marks the
    *smallest* dirty region that's actually correct for what changed:
    plain cursor moves only set dirty_cursor; single-line edits add just
    that row to dirty_rows; anything that can shift multiple rows at once
    (scrolling, resize, erase-whole-screen, alt-screen toggle) sets
    dirty_all instead of trying to compute an exact row range.
    """

    def __init__(self, rows=24, cols=80):
        self.rows = rows
        self.cols = cols
        self.lock = threading.Lock()

        self.grid = self._new_grid(rows, cols)
        self.saved_main_grid = None
        self.saved_main_cursor = (0, 0)
        self.using_alt = False

        self.cur_row = 0
        self.cur_col = 0
        self.cur_fg = None
        self.cur_bg = None
        self.cur_bold = False
        self.cur_underline = False
        self.cur_reverse = False
        self.saved_cursor = None

        self.scroll_top = 0
        self.scroll_bottom = rows - 1
        self.cursor_visible = True

        # -- scrollback (primary screen only) --
        self.scrollback = deque(maxlen=SCROLLBACK_MAX)
        self.scroll_offset = 0  # 0 = live/bottom; N = N lines back into history

        # -- dirty tracking --
        self.dirty_all = True
        self.dirty_rows = set()
        self.dirty_cursor = True
        self.view_dirty = True

    # -- construction helpers ------------------------------------------------

    @staticmethod
    def _new_grid(rows, cols):
        return [[Cell() for _ in range(cols)] for _ in range(rows)]

    # -- dirty tracking -------------------------------------------------

    def mark_dirty(self, row=None):
        """row=None marks the whole screen (structural change); an int
        marks just that grid row's content as needing a redraw."""
        if row is None:
            self.dirty_all = True
        else:
            self.dirty_rows.add(row)

    def mark_rows_dirty(self, start, end):
        for r in range(max(0, start), min(self.rows, end)):
            self.dirty_rows.add(r)

    def mark_cursor_dirty(self):
        self.dirty_cursor = True

    def is_dirty(self):
        return bool(self.dirty_all or self.dirty_rows or self.dirty_cursor or self.view_dirty)

    def pop_dirty(self):
        """Atomically reads and clears all dirty state. Call once per
        throttled redraw tick."""
        with self.lock:
            info = {
                "all": self.dirty_all,
                "rows": self.dirty_rows,
                "cursor": self.dirty_cursor,
                "view": self.view_dirty,
            }
            self.dirty_all = False
            self.dirty_rows = set()
            self.dirty_cursor = False
            self.view_dirty = False
            return info

    # -- plain text -----------------------------------------------------

    def put_char(self, ch):
        with self.lock:
            if self.cur_col >= self.cols:
                self.cur_col = 0
                self._line_feed_locked()
            self.grid[self.cur_row][self.cur_col] = Cell(
                ch, self.cur_fg, self.cur_bg, self.cur_bold, self.cur_underline, self.cur_reverse
            )
            self.mark_dirty(self.cur_row)
            self.cur_col += 1
            self.mark_cursor_dirty()

    def carriage_return(self):
        with self.lock:
            self.cur_col = 0
            self.mark_cursor_dirty()

    def backspace(self):
        with self.lock:
            self.cur_col = max(0, self.cur_col - 1)
            self.mark_cursor_dirty()

    def tab(self):
        with self.lock:
            self.cur_col = min(self.cols - 1, (self.cur_col // 8 + 1) * 8)
            self.mark_cursor_dirty()

    def line_feed(self):
        with self.lock:
            self._line_feed_locked()
            self.mark_cursor_dirty()

    def reverse_line_feed(self):
        with self.lock:
            if self.cur_row == self.scroll_top:
                self._scroll_down_locked(1)
            else:
                self.cur_row = max(0, self.cur_row - 1)
            self.mark_cursor_dirty()

    def _line_feed_locked(self):
        if self.cur_row == self.scroll_bottom:
            self._scroll_up_locked(1)
        else:
            self.cur_row = min(self.rows - 1, self.cur_row + 1)

    def _scroll_up_locked(self, n):
        for _ in range(n):
            removed = self.grid[self.scroll_top]
            # Only lines leaving the *top of the whole primary screen* count
            # as real scrollback history (not a partial DECSTBM region, and
            # never while an app owns the alternate screen).
            if not self.using_alt and self.scroll_top == 0:
                self.scrollback.append([c.copy() for c in removed])
            del self.grid[self.scroll_top]
            self.grid.insert(self.scroll_bottom, [Cell() for _ in range(self.cols)])
        self.mark_dirty(None)

    def _scroll_down_locked(self, n):
        for _ in range(n):
            del self.grid[self.scroll_bottom]
            self.grid.insert(self.scroll_top, [Cell() for _ in range(self.cols)])
        self.mark_dirty(None)

    def scroll_up(self, n):
        with self.lock:
            self._scroll_up_locked(n)

    def scroll_down(self, n):
        with self.lock:
            self._scroll_down_locked(n)

    # -- cursor movement --------------------------------------------------

    def cursor_up(self, n):
        with self.lock:
            self.cur_row = max(0, self.cur_row - n)
            self.mark_cursor_dirty()

    def cursor_down(self, n):
        with self.lock:
            self.cur_row = min(self.rows - 1, self.cur_row + n)
            self.mark_cursor_dirty()

    def cursor_forward(self, n):
        with self.lock:
            self.cur_col = min(self.cols - 1, self.cur_col + n)
            self.mark_cursor_dirty()

    def cursor_back(self, n):
        with self.lock:
            self.cur_col = max(0, self.cur_col - n)
            self.mark_cursor_dirty()

    def cursor_position(self, row, col):
        with self.lock:
            self.cur_row = min(self.rows - 1, max(0, row - 1))
            self.cur_col = min(self.cols - 1, max(0, col - 1))
            self.mark_cursor_dirty()

    def cursor_col_absolute(self, col):
        with self.lock:
            self.cur_col = min(self.cols - 1, max(0, col - 1))
            self.mark_cursor_dirty()

    def cursor_row_absolute(self, row):
        with self.lock:
            self.cur_row = min(self.rows - 1, max(0, row - 1))
            self.mark_cursor_dirty()

    def save_cursor(self):
        with self.lock:
            self.saved_cursor = (
                self.cur_row, self.cur_col, self.cur_fg, self.cur_bg,
                self.cur_bold, self.cur_underline, self.cur_reverse,
            )

    def restore_cursor(self):
        with self.lock:
            if self.saved_cursor:
                (self.cur_row, self.cur_col, self.cur_fg, self.cur_bg,
                 self.cur_bold, self.cur_underline, self.cur_reverse) = self.saved_cursor
            self.mark_cursor_dirty()

    def set_scroll_region(self, top, bottom):
        with self.lock:
            self.scroll_top = max(0, min(self.rows - 1, top - 1))
            self.scroll_bottom = max(self.scroll_top, min(self.rows - 1, bottom - 1))
            self.cur_row = self.scroll_top
            self.cur_col = 0
            self.mark_cursor_dirty()

    # -- erasing ----------------------------------------------------------

    def _clear_line_from(self, row, start, end):
        for c in range(start, min(end, self.cols)):
            self.grid[row][c] = Cell()

    def erase_in_display(self, mode):
        with self.lock:
            if mode == 0:
                self._clear_line_from(self.cur_row, self.cur_col, self.cols)
                for r in range(self.cur_row + 1, self.rows):
                    self.grid[r] = [Cell() for _ in range(self.cols)]
                self.mark_rows_dirty(self.cur_row, self.rows)
            elif mode == 1:
                self._clear_line_from(self.cur_row, 0, self.cur_col + 1)
                for r in range(0, self.cur_row):
                    self.grid[r] = [Cell() for _ in range(self.cols)]
                self.mark_rows_dirty(0, self.cur_row + 1)
            else:  # 2 (whole screen) or 3 (+ scrollback, which we don't keep)
                self.grid = self._new_grid(self.rows, self.cols)
                self.mark_dirty(None)

    def erase_in_line(self, mode):
        with self.lock:
            if mode == 0:
                self._clear_line_from(self.cur_row, self.cur_col, self.cols)
            elif mode == 1:
                self._clear_line_from(self.cur_row, 0, self.cur_col + 1)
            else:
                self._clear_line_from(self.cur_row, 0, self.cols)
            self.mark_dirty(self.cur_row)

    # -- SGR (colors/attributes) ------------------------------------------

    def set_sgr(self, nums):
        # Attribute-only change: takes effect on the *next* put_char, which
        # will mark its own row dirty - nothing on screen changes yet.
        with self.lock:
            i = 0
            n = len(nums)
            while i < n:
                code = nums[i]
                if code == 0:
                    self.cur_fg = None
                    self.cur_bg = None
                    self.cur_bold = self.cur_underline = self.cur_reverse = False
                elif code == 1:
                    self.cur_bold = True
                elif code == 4:
                    self.cur_underline = True
                elif code == 7:
                    self.cur_reverse = True
                elif code == 22:
                    self.cur_bold = False
                elif code == 24:
                    self.cur_underline = False
                elif code == 27:
                    self.cur_reverse = False
                elif 30 <= code <= 37:
                    self.cur_fg = ANSI_COLORS[code - 30]
                elif code == 38:
                    if i + 1 < n and nums[i + 1] == 5 and i + 2 < n:
                        self.cur_fg = palette_256(nums[i + 2])
                        i += 2
                    elif i + 1 < n and nums[i + 1] == 2 and i + 4 < n:
                        self.cur_fg = (nums[i + 2], nums[i + 3], nums[i + 4])
                        i += 4
                elif code == 39:
                    self.cur_fg = None
                elif 40 <= code <= 47:
                    self.cur_bg = ANSI_COLORS[code - 40]
                elif code == 48:
                    if i + 1 < n and nums[i + 1] == 5 and i + 2 < n:
                        self.cur_bg = palette_256(nums[i + 2])
                        i += 2
                    elif i + 1 < n and nums[i + 1] == 2 and i + 4 < n:
                        self.cur_bg = (nums[i + 2], nums[i + 3], nums[i + 4])
                        i += 4
                elif code == 49:
                    self.cur_bg = None
                elif 90 <= code <= 97:
                    self.cur_fg = ANSI_BRIGHT_COLORS[code - 90]
                elif 100 <= code <= 107:
                    self.cur_bg = ANSI_BRIGHT_COLORS[code - 100]
                # unknown SGR codes are silently ignored
                i += 1

    # -- modes --------------------------------------------------------------

    def set_private_mode(self, code, enable):
        with self.lock:
            if code == 25:
                self.cursor_visible = enable
                self.mark_cursor_dirty()
            elif code in (1049, 47, 1047):
                if enable:
                    self._enter_alt_screen_locked()
                else:
                    self._exit_alt_screen_locked()
                self.mark_dirty(None)

    def _enter_alt_screen_locked(self):
        if not self.using_alt:
            self.saved_main_grid = self.grid
            self.saved_main_cursor = (self.cur_row, self.cur_col)
            self.grid = self._new_grid(self.rows, self.cols)
            self.cur_row = 0
            self.cur_col = 0
            self.using_alt = True
            # Scrollback is a primary-screen-only concept; always show the
            # live alt-screen while an app owns it.
            if self.scroll_offset != 0:
                self.scroll_offset = 0
                self.view_dirty = True

    def _exit_alt_screen_locked(self):
        if self.using_alt and self.saved_main_grid is not None:
            self.grid = self.saved_main_grid
            self.cur_row, self.cur_col = self.saved_main_cursor
            self.saved_main_grid = None
            self.using_alt = False

    def reset(self):
        with self.lock:
            self.grid = self._new_grid(self.rows, self.cols)
            self.cur_row = 0
            self.cur_col = 0
            self.cur_fg = None
            self.cur_bg = None
            self.cur_bold = self.cur_underline = self.cur_reverse = False
            self.scroll_top = 0
            self.scroll_bottom = self.rows - 1
            self.mark_dirty(None)

    # -- scrollback viewport --------------------------------------------

    def scroll_view(self, delta_lines):
        """delta_lines > 0 moves toward history (up), < 0 moves toward the
        live bottom. No-op while the alternate screen is active, matching
        real terminals (nano/vim/htop own the whole viewport)."""
        with self.lock:
            if self.using_alt:
                return
            max_offset = len(self.scrollback)
            new_offset = max(0, min(max_offset, self.scroll_offset + delta_lines))
            if new_offset != self.scroll_offset:
                self.scroll_offset = new_offset
                self.view_dirty = True

    def scroll_to_bottom(self):
        with self.lock:
            if self.scroll_offset != 0:
                self.scroll_offset = 0
                self.view_dirty = True

    # -- resize (called when the widget/window changes size) ---------------

    def resize(self, rows, cols):
        rows = max(1, rows)
        cols = max(1, cols)
        with self.lock:
            if rows == self.rows and cols == self.cols:
                return
            new_grid = self._new_grid(rows, cols)
            for r in range(min(rows, self.rows)):
                for c in range(min(cols, self.cols)):
                    new_grid[r][c] = self.grid[r][c]
            self.grid = new_grid
            self.rows = rows
            self.cols = cols
            self.cur_row = min(self.cur_row, rows - 1)
            self.cur_col = min(self.cur_col, cols - 1)
            self.scroll_top = 0
            self.scroll_bottom = rows - 1
            self.mark_dirty(None)

    # -- rendering view ---------------------------------------------------

    def get_visible_rows(self):
        """Returns (rows, cur_row, cur_col, cursor_visible, scroll_offset)
        for whatever should currently be on screen: the live grid (cursor
        row/col meaningful) if at the bottom or in the alt screen, or a
        scrollback+grid slice (cursor hidden - it isn't part of history)
        if the user has scrolled up. Cheap enough to call at a throttled
        redraw rate, not per-byte."""
        with self.lock:
            if self.using_alt or self.scroll_offset == 0:
                rows = list(self.grid)
                return rows, self.cur_row, self.cur_col, self.cursor_visible, 0

            history = list(self.scrollback)
            combined_len = len(history) + len(self.grid)
            end = combined_len - self.scroll_offset
            start = end - self.rows
            rows = []
            blank_row = None
            for i in range(start, end):
                if i < 0:
                    if blank_row is None:
                        blank_row = [Cell() for _ in range(self.cols)]
                    rows.append(blank_row)
                elif i < len(history):
                    rows.append(history[i])
                else:
                    rows.append(self.grid[i - len(history)])
            return rows, None, None, False, self.scroll_offset



# ---------------------------------------------------------------------------
# VTParser - byte stream -> Screen mutations
# ---------------------------------------------------------------------------

class VTParser:
    ST_GROUND = 0
    ST_ESC = 1
    ST_CSI = 2
    ST_OSC = 3
    ST_CHARSET = 4

    def __init__(self, screen: Screen):
        self.screen = screen
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.state = self.ST_GROUND
        self._params = ""
        self._osc_esc_pending = False

    def feed(self, data: bytes):
        """Call from the PTY reader thread with each chunk of raw bytes."""
        text = self._decoder.decode(data)
        for ch in text:
            self._feed_char(ch)

    # -- state machine -------------------------------------------------

    def _feed_char(self, ch):
        s = self.screen
        state = self.state

        if state == self.ST_GROUND:
            if ch == "\x1b":
                self.state = self.ST_ESC
            elif ch == "\r":
                s.carriage_return()
            elif ch == "\n":
                s.line_feed()
            elif ch == "\b":
                s.backspace()
            elif ch == "\t":
                s.tab()
            elif ch == "\x07":
                pass  # BEL - ignored (could trigger a visual/audible bell later)
            elif ord(ch) < 0x20:
                pass  # other C0 control codes - ignored
            else:
                s.put_char(ch)

        elif state == self.ST_ESC:
            if ch == "[":
                self.state = self.ST_CSI
                self._params = ""
            elif ch == "]":
                self.state = self.ST_OSC
                self._osc_esc_pending = False
            elif ch in "()":
                self.state = self.ST_CHARSET
            elif ch == "7":
                s.save_cursor()
                self.state = self.ST_GROUND
            elif ch == "8":
                s.restore_cursor()
                self.state = self.ST_GROUND
            elif ch == "c":
                s.reset()
                self.state = self.ST_GROUND
            elif ch == "D":
                s.line_feed()
                self.state = self.ST_GROUND
            elif ch == "M":
                s.reverse_line_feed()
                self.state = self.ST_GROUND
            else:
                # Unrecognized single-char escape (keypad mode select, etc.)
                # - consume it and return to ground rather than misparsing
                # subsequent bytes as part of it.
                self.state = self.ST_GROUND

        elif state == self.ST_CHARSET:
            # Consume the charset designator byte (e.g. 'B' for US-ASCII in
            # "ESC ( B"). We don't implement alternate character sets.
            self.state = self.ST_GROUND

        elif state == self.ST_CSI:
            if ch.isdigit() or ch in ";?":
                self._params += ch
            elif 0x40 <= ord(ch) <= 0x7E:
                self._dispatch_csi(ch, self._params)
                self.state = self.ST_GROUND
            else:
                # Intermediate byte (rare) - keep collecting.
                self._params += ch

        elif state == self.ST_OSC:
            if ch == "\x07":
                self.state = self.ST_GROUND
            elif self._osc_esc_pending:
                # Previous char was ESC; this should be '\' (ST terminator).
                self.state = self.ST_GROUND
                self._osc_esc_pending = False
            elif ch == "\x1b":
                self._osc_esc_pending = True
            # else: still inside the OSC string payload (title text etc.) - discard it

    # -- CSI dispatch --------------------------------------------------

    def _dispatch_csi(self, final, params_str):
        s = self.screen
        private = params_str.startswith("?")
        body = params_str[1:] if private else params_str
        nums = []
        for part in body.split(";"):
            try:
                nums.append(int(part)) if part != "" else nums.append(0)
            except ValueError:
                nums.append(0)

        def arg(i, default):
            if i < len(nums) and nums[i]:
                return nums[i]
            return default

        if final == "A":
            s.cursor_up(arg(0, 1))
        elif final == "B":
            s.cursor_down(arg(0, 1))
        elif final == "C":
            s.cursor_forward(arg(0, 1))
        elif final == "D":
            s.cursor_back(arg(0, 1))
        elif final in ("H", "f"):
            s.cursor_position(arg(0, 1), arg(1, 1))
        elif final == "G":
            s.cursor_col_absolute(arg(0, 1))
        elif final == "d":
            s.cursor_row_absolute(arg(0, 1))
        elif final == "J":
            s.erase_in_display(nums[0] if nums else 0)
        elif final == "K":
            s.erase_in_line(nums[0] if nums else 0)
        elif final == "m":
            s.set_sgr(nums if nums else [0])
        elif final == "s":
            s.save_cursor()
        elif final == "u":
            s.restore_cursor()
        elif final == "r":
            s.set_scroll_region(arg(0, 1), arg(1, s.rows))
        elif final in ("h", "l"):
            if private:
                enable = final == "h"
                for code in nums:
                    s.set_private_mode(code, enable)
            # non-private ANSI modes (IRM, LNM, etc.) are not implemented
        elif final == "S":
            s.scroll_up(arg(0, 1))
        elif final == "T":
            s.scroll_down(arg(0, 1))
        # Any other final byte (device status reports, etc.) is silently ignored.


# ---------------------------------------------------------------------------
# Rendering helper - turns a Screen snapshot into Kivy BBCode markup text.
# Kept here (rather than in main.py) so it can be unit-tested without Kivy.
# ---------------------------------------------------------------------------

def render_markup(grid, default_fg=DEFAULT_FG):
    """Builds one Kivy-markup string (rows joined by \\n) from a grid
    snapshot. Foreground color, bold, and underline are rendered as spans.
    NOTE: background colors are intentionally not rendered here (Kivy markup
    has no background-color span) - see module docstring."""
    from kivy.utils import escape_markup  # imported lazily so this module

    # stays importable in a plain Python unit test environment without Kivy.

    lines = []
    for row in grid:
        parts = []
        run_text = ""
        run_fg = None
        run_bold = None
        run_underline = None

        def flush():
            nonlocal run_text
            if not run_text:
                return
            fg = run_fg or default_fg
            text = escape_markup(run_text)
            if run_underline:
                text = f"[u]{text}[/u]"
            if run_bold:
                text = f"[b]{text}[/b]"
            parts.append(f"[color=#{fg[0]:02x}{fg[1]:02x}{fg[2]:02x}]{text}[/color]")
            run_text = ""

        for cell in row:
            fg, bg = cell.fg, cell.bg
            if cell.reverse:
                fg, bg = (bg or DEFAULT_BG), (fg or default_fg)
            key = (fg, cell.bold, cell.underline)
            if key != (run_fg, run_bold, run_underline):
                flush()
                run_fg, run_bold, run_underline = key
            run_text += cell.ch if cell.ch else " "
        flush()
        lines.append("".join(parts) if parts else "")
    return "\n".join(lines)
