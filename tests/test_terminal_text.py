"""Tests for terminal_text() / terminal_line() — the display escapers that
neutralize untrusted message bodies before they hit a terminal.

This is a security control: a hostile body must not be able to inject ANSI/CSI
or OSC control sequences that move the cursor, rewrite the scrollback, retitle
the window, or smuggle a paste. terminal_text() hex-escapes every control byte
except the whitespace that is safe to render (\\n, \\t); terminal_line() is the
one-line variant used for headers, so it additionally folds newlines to spaces.

Pure-function tests: no Redis, no gh, no network. The `bus` script has no .py
extension, so we load it as a module by path (mirrors tests/test_status_transition.py).

Run:  python -m unittest discover -s tests   (from the repo root)
"""
import importlib.machinery
import importlib.util
import os
import unittest

_BUS_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "bus")


def _load_bus():
    # `bus` has no .py extension, so name a SourceFileLoader explicitly rather
    # than relying on extension-based loader inference (which yields spec=None).
    loader = importlib.machinery.SourceFileLoader("busmod", _BUS_PATH)
    spec = importlib.util.spec_from_loader("busmod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


bus = _load_bus()
text = bus.terminal_text
line = bus.terminal_line


class TerminalText(unittest.TestCase):
    def test_plain_ascii_passes_through_untouched(self):
        for s in ("hello world", "issue-42", "PR #7 ready for review",
                  "a.b_c/d-e:f", "1234567890", "~!@#$%^&*()"):
            self.assertEqual(text(s), s, s)

    def test_empty_string(self):
        self.assertEqual(text(""), "")

    def test_newline_and_tab_preserved(self):
        # the two whitespace controls that are safe to render literally
        self.assertEqual(text("a\nb"), "a\nb")
        self.assertEqual(text("a\tb"), "a\tb")
        self.assertEqual(text("line1\nline2\tcol"), "line1\nline2\tcol")

    def test_space_is_printable_not_escaped(self):
        self.assertEqual(text("   "), "   ")

    def test_carriage_return_escaped(self):
        # \r (0x0d) is NOT in the {\n,\t} allowlist — a bare CR rewrites the
        # current line, so it must be neutralized.
        self.assertEqual(text("a\rb"), "a\\x0db")

    def test_null_and_bell_and_backspace_escaped(self):
        self.assertEqual(text("\x00"), "\\x00")   # NUL
        self.assertEqual(text("\x07"), "\\x07")   # BEL
        self.assertEqual(text("\x08"), "\\x08")   # BS

    def test_esc_byte_escaped(self):
        # the lone ESC (0x1b) that begins every CSI/OSC sequence
        self.assertEqual(text("\x1b"), "\\x1b")

    def test_ansi_csi_color_sequence_hex_escaped(self):
        # ESC [ 3 1 m  — a "turn text red" SGR sequence. Only the ESC is a
        # control byte; the rest are printable and pass through, so the whole
        # sequence is defanged (no live ESC to start it).
        self.assertEqual(text("\x1b[31mred\x1b[0m"),
                         "\\x1b[31mred\\x1b[0m")
        self.assertNotIn("\x1b", text("\x1b[31mred\x1b[0m"))

    def test_ansi_cursor_move_sequence_hex_escaped(self):
        # ESC [ 2 J (clear screen) + ESC [ H (home) — a classic scrollback wipe.
        out = text("\x1b[2J\x1b[H")
        self.assertEqual(out, "\\x1b[2J\\x1b[H")
        self.assertNotIn("\x1b", out)

    def test_osc_window_title_sequence_hex_escaped(self):
        # ESC ] 0 ; pwned BEL — an OSC that retitles the terminal window. Both
        # the ESC and the BEL terminator are escaped.
        out = text("\x1b]0;pwned\x07")
        self.assertEqual(out, "\\x1b]0;pwned\\x07")
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)

    def test_c1_control_bytes_escaped(self):
        # 0x80-0x9f: the C1 control range. Some terminals treat a lone 0x9b as
        # a CSI introducer, so these must never pass through.
        self.assertEqual(text("\x80"), "\\x80")
        self.assertEqual(text("\x9b"), "\\x9b")   # CSI (single-byte form)
        self.assertEqual(text("\x9f"), "\\x9f")

    def test_c1_boundaries(self):
        # 0x7f (DEL) and the whole 0x80-0x9f band escape; 0xa0 (just past the
        # band, a printable NBSP) passes through.
        self.assertEqual(text("\x7f"), "\\x7f")   # DEL
        self.assertEqual(text("\x9f"), "\\x9f")   # top of C1 band
        self.assertEqual(text("\xa0"), "\xa0")    # first codepoint above it

    def test_printable_unicode_passes_through(self):
        # non-control codepoints above 0x9f are not the escaper's job to touch
        for s in ("café", "naïve", "日本語", "emoji 🚀", "Ω≈ç√"):
            self.assertEqual(text(s), s, s)

    def test_non_string_input_is_stringified(self):
        # str(value) coercion: emit()/callers pass ints and None through
        self.assertEqual(text(42), "42")
        self.assertEqual(text(None), "None")

    def test_hostile_body_fully_neutralized(self):
        # A realistic attack body: hide the real content, wipe the screen, spoof
        # a fake prompt line, and retitle the window. After escaping there must
        # be no live ESC / BEL / CR left to drive the terminal.
        hostile = ("\x1b[2J\x1b[H"                # clear screen, home cursor
                   "\x1b]0;you-are-pwned\x07"     # OSC window-title spoof
                   "$ rm -rf ~\r"                 # CR to overwrite the line
                   "\x1b[31mALERT\x1b[0m")        # colored fake alert
        out = text(hostile)
        for raw in ("\x1b", "\x07", "\r"):
            self.assertNotIn(raw, out, repr(raw))
        # the escaped forms are present and the human-readable text survives
        self.assertIn("\\x1b[2J", out)
        self.assertIn("\\x07", out)
        self.assertIn("\\x0d", out)
        self.assertIn("rm -rf", out)
        self.assertIn("ALERT", out)


class TerminalLine(unittest.TestCase):
    def test_plain_text_unchanged(self):
        self.assertEqual(line("hello world"), "hello world")

    def test_newline_folded_to_space(self):
        # the one behavioral difference from terminal_text: headers are single
        # line, so a literal newline becomes a space.
        self.assertEqual(line("a\nb"), "a b")
        self.assertEqual(line("l1\nl2\nl3"), "l1 l2 l3")

    def test_tab_is_preserved_not_folded(self):
        # terminal_line only rewrites '\n'; tab survives (terminal_text kept it).
        self.assertEqual(line("a\tb"), "a\tb")

    def test_control_bytes_still_escaped(self):
        # terminal_line delegates the escaping to terminal_text, so CR/ESC/etc.
        # are neutralized exactly the same way.
        self.assertEqual(line("a\rb"), "a\\x0db")
        self.assertEqual(line("\x1b[31mx"), "\\x1b[31mx")

    def test_escaped_newline_marker_is_not_folded(self):
        # only a *real* newline byte is folded; the literal backslash-x-0a that
        # an escaped control produces stays intact (there is no raw \n left).
        self.assertEqual(line("\x85"), "\\x85")   # NEL (a C1 control), escaped

    def test_hostile_header_neutralized_and_single_line(self):
        out = line("evil\x1b]0;t\x07\nsecond")
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertNotIn("\n", out)          # folded to a space
        self.assertIn("evil", out)
        self.assertIn("second", out)


if __name__ == "__main__":
    unittest.main()
