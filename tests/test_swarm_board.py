"""Pilot tests for scripts/swarm-board against an in-memory fakeredis.

Run:  python -m unittest tests.test_swarm_board -v   (needs textual + fakeredis)
"""
import importlib.machinery
import importlib.util
import os
import time
import unittest

try:
    import fakeredis
    import textual  # noqa: F401
except ImportError:  # pragma: no cover - suite skips cleanly without the deps
    fakeredis = None

_BOARD_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "scripts", "swarm-board")
ROOM = "swarm-test"
SIZE = (120, 50)
BASE_MS = int(time.time() * 1000) - 600_000


def _load_board():
    loader = importlib.machinery.SourceFileLoader("swarm_board_mod", _BOARD_PATH)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


board = _load_board() if fakeredis else None

POSTS = [
    ("coordinator", "all", "Plan for @w1 and @verifier\n- first step\n- second step"),
    ("w1", "coordinator", "Working on `scripts/swarm-board` now, **bold** claim"),
    ("verifier", "all", "Waiting for [bold red]markup[/] and \x1b[31mescapes"),
    ("w1", "all", "Done: " + "word " * 60),
]


def text_of(widget):
    return widget.content.plain


@unittest.skipIf(fakeredis is None, "fakeredis/textual not installed")
class SwarmBoardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.r = fakeredis.FakeStrictRedis(decode_responses=True)
        self.r.hset(f"bus:meta:{ROOM}", "title", "Epic 62")
        self.member("coordinator", role="coordinator", model="claude-opus", state="waiting")
        self.member("w1", role="worker", model="claude-sonnet-5-5", state="working",
                    workspace="/tmp/does-not-exist", conversation="sess-1")
        self.member("verifier", role="verifier", model="gpt-5", state="waiting")
        for i, (sender, to, body) in enumerate(POSTS):
            self.post(i, sender, to, body)

    def member(self, name, **fields):
        self.r.hset(f"bus:member:{ROOM}:{name}", mapping=fields)
        self.r.set(f"bus:presence:{ROOM}:{name}", "now", ex=90)

    def post(self, index, sender, to, body, kind="msg"):
        self.r.xadd(f"bus:stream:{ROOM}", {"from": sender, "to": to, "body": body, "kind": kind},
                    id=f"{BASE_MS + index * 60_000}-0")

    def make_app(self, interval=0, opener=None):
        return board.SwarmBoard(self.r, ROOM, interval=interval, opener=opener)

    async def load(self, app, pilot):
        app.apply(board.fetch(self.r, ROOM, app.state.last_id, len(app.state.posts)))
        await pilot.pause()

    async def test_header_roster_and_posts(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            self.assertEqual(text_of(app.query_one("#header")),
                             'Swarm board "Epic 62" · 4 posts · 3 members · [follow: on]')
            self.assertEqual(text_of(app.query_one("#roster-label")), "Roster · 3 members, 2 waiting")
            roster = text_of(app.query_one("#roster"))
            self.assertIn("● working", roster)
            self.assertEqual(roster.count("○ waiting"), 2)
            self.assertIn("♛", roster)
            self.assertEqual(text_of(app.query_one("#posts-label")), "Posts · 4 total")
            rows = text_of(app.query_one("#posts")).split("\n")
            self.assertEqual(len(rows), 4)
            self.assertTrue(rows[3].startswith("> #4"))
            self.assertIn("#1", rows[0])
            self.assertIn("coordinator", rows[0])
            self.assertIn("Plan for @w1 and @verifier", rows[0])

    async def test_overlapping_snapshots_do_not_duplicate_posts(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            app.apply(board.fetch(self.r, ROOM, "-", 0))
            await pilot.pause()
            self.assertEqual([p.seq for p in app.state.posts], [1, 2, 3, 4])

    async def test_colors_distinct_for_first_six_members_and_stable(self):
        for name in ("a1", "a2", "a3", "a4"):
            self.member(name, role="worker", state="waiting")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            names = app.state.roster()
            colors = [app.state.color(n) for n in names]
            self.assertEqual(len(names), 7)
            self.assertEqual(len(set(colors[:6])), 6)
            self.assertEqual(app.state.color("coordinator"), board.GOLD)
            self.member("aaa-new", role="worker", state="waiting")
            await self.load(app, pilot)
            self.assertEqual([app.state.color(n) for n in names], colors)
            self.assertNotIn(app.state.color("aaa-new"), colors[:6])

    async def test_header_totals_and_card_usage(self):
        self.r.hset(f"bus:member:{ROOM}:w1", mapping={"tokens_in": 1_000_000, "tokens_out": 234_567, "cost_usd": 1.5})
        self.r.hset(f"bus:member:{ROOM}:coordinator", mapping={"tokens_in": 100_000, "tokens_out": 28_000, "cost_usd": 0.42})
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            self.assertEqual(text_of(app.query_one("#header")),
                             'Swarm board "Epic 62" · 4 posts · 3 members · 1.4M tok · $1.92 · [follow: on]')
            first = text_of(app.query_one("#roster")).split("\n")[0]
            self.assertIn("128k tok · $0.42", first)
            self.assertIn("1.2M tok · $1.50", first)
            self.assertEqual(first.count("tok"), 2)

    async def test_card_lines_and_doing_fallback(self):
        self.r.hset(f"bus:member:{ROOM}:w1", mapping={"harness": "codex", "doing": "editing the board",
                                                      "state_at": board.bus.now_iso()})
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            lines = text_of(app.query_one("#roster")).split("\n")
            self.assertEqual(len(lines), 4)
            self.assertRegex(lines[1], r"● working \d+s")
            self.assertIn("codex · claude-sonnet-5-5", lines[2])
            self.assertIn("editing the board", lines[3])
            self.assertIn("Plan for @w1 and @verifier", lines[3])
            self.assertIn("[bold red]markup[/]", lines[3])

    async def test_detail_shows_usage_groups_and_harness(self):
        self.r.hset(f"bus:member:{ROOM}:w1", mapping={"harness": "omp", "tokens_in": 1234, "tokens_out": 56,
                                                      "turns": 7, "cost_usd": 0.5})
        self.r.sadd(f"bus:groups:{ROOM}:w1", "api", "ui")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("tab", "tab", "j", "j")
            detail = text_of(app.query_one("#detail"))
            self.assertIn("harness       omp", detail)
            self.assertIn("@api @ui", detail)
            self.assertIn("in 1.2k / out 56 · 7 turns · $0.50", detail)
            self.assertIn("working", detail)

    async def test_recipients_shown_after_sender_unless_all(self):
        self.post(30, "coordinator", "@api,verifier", "fan out")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            rows = text_of(app.query_one("#posts")).split("\n")
            self.assertNotIn("→", rows[0])
            self.assertIn("w1 → coordinator", rows[1])
            self.assertIn("coordinator → @api,verifier", rows[4])

    async def test_retired_members_collapse_under_the_grid(self):
        self.r.hset(f"bus:member:{ROOM}:w1", "state", "retired")
        self.r.hset(f"bus:member:{ROOM}:verifier", "state", "retired")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            roster = text_of(app.query_one("#roster"))
            self.assertIn("Retired (2): verifier, w1", roster.split("\n")[-1])
            self.assertNotIn("gpt-5", roster)
            self.assertEqual(text_of(app.query_one("#roster-label")), "Roster · 1 member, 1 waiting · all on claude-opus")
            self.assertEqual(text_of(app.query_one("#header")).count("3 members"), 1)

    async def test_all_on_model_only_when_active_members_share_one(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            self.assertNotIn("all on", text_of(app.query_one("#roster-label")))
            self.r.hset(f"bus:member:{ROOM}:w1", "state", "retired")
            self.r.hset(f"bus:member:{ROOM}:verifier", "model", "claude-opus")
            await self.load(app, pilot)
            self.assertEqual(text_of(app.query_one("#roster-label")), "Roster · 2 members, 2 waiting · all on claude-opus")

    async def test_kind_tags_in_rows_and_message_header(self):
        self.post(5, "coordinator", "all", "all finished", kind="done")
        self.post(6, "verifier", "all", "tick", kind="system")
        self.post(7, "w1", "coordinator", "which branch?", kind="question")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            rows = text_of(app.query_one("#posts")).split("\n")
            self.assertIn("✓ done all finished", rows[4])
            self.assertIn("⚙ tick", rows[5])
            self.assertIn("? which branch?", rows[6])
            self.assertNotIn("✓", rows[0])
            await pilot.press("k", "k")
            self.assertRegex(text_of(app.query_one("#message-label")),
                             r"^Message · #5 coordinator ✓ done · \d\d:\d\d:\d\d · lines ")

    async def test_older_posts_show_date_and_columns_stay_aligned(self):
        self.r.delete(f"bus:stream:{ROOM}")
        day_ms = 86_400_000
        self.r.xadd(f"bus:stream:{ROOM}", {"from": "coordinator", "to": "all", "body": "old"}, id=f"{BASE_MS - 3 * day_ms}-0")
        self.post(1, "w1", "all", "new")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            rows = text_of(app.query_one("#posts")).split("\n")
            self.assertRegex(rows[0], r"#1\s+[A-Z][a-z]{2} \d{2} \d{2}:\d{2}  coordinator")
            self.assertEqual(rows[0].index("coordinator"), rows[1].index("w1"))

    async def test_posts_label_shows_window_when_scrolling(self):
        for i in range(10, 50):
            self.post(i, "w1", "all", f"filler {i}")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            label = text_of(app.query_one("#posts-label"))
            self.assertRegex(label, r"^Posts · 44 total · rows (\d+)-44 of 44$")
            first = int(label.split("rows ")[1].split("-")[0])
            self.assertGreater(first, 1)
            self.assertEqual(len(text_of(app.query_one("#posts")).split("\n")), 44 - first + 1)

    def selected_row_visible(self, app):
        pane = app.query_one("#posts")
        lines = pane.content.plain.split("\n")
        self.assertLessEqual(len(lines), pane.size.height)
        self.assertTrue(any(line.startswith("> ") for line in lines), lines)

    async def test_selected_row_is_always_scrolled_into_view(self):
        for i in range(10, 40):
            self.post(i, "w1", "all", f"filler {i}")
        app = self.make_app()
        async with app.run_test(size=(120, 36)) as pilot:
            await self.load(app, pilot)
            await pilot.pause()
            self.assertLess(app.query_one("#posts").size.height, len(app.state.posts))
            self.selected_row_visible(app)
            for _ in range(len(app.state.posts) - 1):
                await pilot.press("k")
                self.selected_row_visible(app)
            for _ in range(5):
                await pilot.press("j")
                self.selected_row_visible(app)
            await pilot.press("f")
            self.post(50, "verifier", "all", "newest")
            await self.load(app, pilot)
            self.selected_row_visible(app)
            self.assertIn("newest", text_of(app.query_one("#posts")).split("\n")[-1])

    async def test_sender_and_recipients_column_is_capped(self):
        self.post(5, "w1", "@coordinator,@verifier,@someone-with-a-long-name", "BODYX")
        self.post(6, "coordinator", "all", "BODYY")
        self.post(7, "a-very-long-sender-name-that-keeps-going", "@coordinator", "BODYZ")
        app = self.make_app()
        async with app.run_test(size=(140, 60)) as pilot:
            await self.load(app, pilot)
            rows = text_of(app.query_one("#posts")).split("\n")
            by_tag = {tag: next(row for row in rows if tag in row) for tag in ("BODYX", "BODYY", "BODYZ")}
            self.assertEqual(len({row.index(tag) for tag, row in by_tag.items()}), 1)
            self.assertIn("w1 → @coordinator,@verifier…", by_tag["BODYX"])
            self.assertIn("…", by_tag["BODYX"])
            self.assertRegex(by_tag["BODYZ"], r"a-very-long-sender-n… → @co…  BODYZ")
            body_col = by_tag["BODYX"].index("BODYX")
            self.assertEqual(body_col - by_tag["BODYX"].index("w1"), board.SENDER_CELL_WIDTH + 2)

    async def test_singular_member_and_post_in_header_and_label(self):
        self.r.delete(f"bus:stream:{ROOM}")
        for name in ("w1", "verifier"):
            self.r.delete(f"bus:member:{ROOM}:{name}", f"bus:presence:{ROOM}:{name}")
        self.post(0, "coordinator", "all", "solo")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            self.assertEqual(text_of(app.query_one("#header")),
                             'Swarm board "Epic 62" · 1 post · 1 member · [follow: on]')
            self.assertEqual(text_of(app.query_one("#roster-label")),
                             "Roster · 1 member, 1 waiting · all on claude-opus")

    async def test_posts_label_has_no_window_when_everything_fits(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            self.assertEqual(text_of(app.query_one("#posts-label")), "Posts · 4 total")

    async def test_finished_header_when_latest_post_is_done(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            self.assertNotIn("finished", text_of(app.query_one("#header")))
            self.post(5, "coordinator", "all", "ship it", kind="done")
            await self.load(app, pilot)
            self.assertEqual(text_of(app.query_one("#header")),
                             'Swarm board "Epic 62" · 5 posts · ✓ finished · 3 members · [follow: on]')
            self.post(6, "w1", "all", "late reply")
            await self.load(app, pilot)
            self.assertNotIn("finished", text_of(app.query_one("#header")))

    async def test_done_from_a_non_coordinator_is_not_finished(self):
        self.post(5, "w1", "all", "I declare victory", kind="done")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            self.assertNotIn("finished", text_of(app.query_one("#header")))

    async def test_working_without_presence_is_gone(self):
        self.r.delete(f"bus:presence:{ROOM}:w1")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            roster = text_of(app.query_one("#roster"))
            self.assertIn("○ gone", roster)
            self.assertNotIn("● working", roster)
            self.assertEqual(text_of(app.query_one("#roster-label")), "Roster · 3 members, 3 waiting")

    async def test_title_falls_back_to_room(self):
        self.r.delete(f"bus:meta:{ROOM}")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            self.assertIn(f'"{ROOM}"', text_of(app.query_one("#header")))

    async def test_selecting_post_shows_full_body(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("k", "k", "k")
            label = text_of(app.query_one("#message-label"))
            self.assertTrue(label.startswith("Message · #1 coordinator · "))
            body = text_of(app.query_one("#message"))
            self.assertIn("Plan for @w1 and @verifier", body)
            self.assertIn("- first step", body)
            self.assertIn("- second step", body)
            self.assertIn("Post 1/4 (seq #1 by coordinator at", text_of(app.query_one("#status")))
            self.assertFalse(app.state.follow)

    async def test_untrusted_text_is_shown_literally(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("k")
            body = text_of(app.query_one("#message"))
            self.assertIn("[bold red]markup[/]", body)
            self.assertIn("\\x1b[31m", body)
            self.assertNotIn("\x1b", body)

    async def test_markdown_highlighting_styles_code_and_mentions(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("k", "k", "k")
            spans = {app.query_one("#message").content.plain[s.start:s.end]: str(s.style)
                     for s in app.query_one("#message").content.spans}
            self.assertEqual(spans["@w1"], app.state.color("w1"))
            await pilot.press("j")
            spans = {app.query_one("#message").content.plain[s.start:s.end]: str(s.style)
                     for s in app.query_one("#message").content.spans}
            self.assertEqual(spans["scripts/swarm-board"], board.BLUE)
            self.assertEqual(spans["bold"], "bold")

    async def test_follow_picks_up_new_post(self):
        app = self.make_app(interval=0.05)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause(0.3)
            self.assertEqual(len(app.state.posts), 4)
            self.post(10, "verifier", "all", "fresh news")
            for _ in range(60):
                await pilot.pause(0.05)
                if len(app.state.posts) == 5:
                    break
            self.assertEqual(len(app.state.posts), 5)
            self.assertIn("5 posts", text_of(app.query_one("#header")))
            self.assertIn("[follow: on]", text_of(app.query_one("#header")))
            self.assertEqual(app.state.selected_post().body, "fresh news")
            self.assertTrue(text_of(app.query_one("#message-label")).startswith("Message · #5 verifier"))

    async def test_new_post_keeps_selection_when_follow_off(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("f", "k")
            self.assertEqual(app.state.sel, 2)
            self.post(10, "verifier", "all", "fresh news")
            await self.load(app, pilot)
            self.assertEqual(len(app.state.posts), 5)
            self.assertEqual(app.state.sel, 2)
            self.assertIn("[follow: off]", text_of(app.query_one("#header")))

    async def test_filter_narrows_posts_to_one_member(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("p")
            self.assertEqual(app.state.filter, "coordinator")
            self.assertEqual(len(text_of(app.query_one("#posts")).split("\n")), 1)
            await pilot.press("p")
            self.assertEqual(app.state.filter, "verifier")
            await pilot.press("p")
            self.assertEqual(app.state.filter, "w1")
            rows = text_of(app.query_one("#posts")).split("\n")
            self.assertEqual(len(rows), 2)
            self.assertTrue(all("w1" in row for row in rows))
            self.assertIn("#2", rows[0])
            self.assertIn("#4", rows[1])
            self.assertIn("sender: w1 (2)", text_of(app.query_one("#posts-label")))
            await pilot.press("p")
            self.assertIsNone(app.state.filter)
            self.assertEqual(len(text_of(app.query_one("#posts")).split("\n")), 4)

    async def test_escape_clears_filter(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("p", "escape")
            self.assertIsNone(app.state.filter)

    async def test_space_expands_selected_post(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("k", "k", "k", "space")
            rows = text_of(app.query_one("#posts")).split("\n")
            self.assertIn("      - first step", rows)

    async def test_tab_cycles_focus_and_roster_moves_member(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("tab")
            self.assertEqual(app.state.focus, "message")
            await pilot.press("tab")
            self.assertEqual(app.state.focus, "roster")
            await pilot.press("j")
            detail = text_of(app.query_one("#detail"))
            self.assertIn("verifier", detail.split("\n")[0])
            self.assertIn("gpt-5", detail)
            await pilot.press("k")
            detail = text_of(app.query_one("#detail"))
            self.assertIn("coordinator", detail.split("\n")[0])
            self.assertIn("last post", detail)

    async def test_detail_block_for_member(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("tab", "tab", "j", "j")
            detail = text_of(app.query_one("#detail"))
            self.assertIn("w1", detail.split("\n")[0])
            self.assertIn("claude-sonnet-5-5", detail)
            self.assertIn("sess-1", detail)
            self.assertIn("/tmp/does-not-exist", detail)
            self.assertIn("#4, 7m ago", detail)

    async def test_enter_hides_message_and_z_zooms(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("enter")
            self.assertFalse(app.query_one("#msg-sec").display)
            await pilot.press("enter")
            self.assertTrue(app.query_one("#msg-sec").display)
            await pilot.press("z")
            self.assertFalse(app.query_one("#top").display)
            self.assertFalse(app.query_one("#posts-sec").display)
            await pilot.press("escape")
            self.assertTrue(app.query_one("#top").display)

    async def test_open_uses_workspace_directory_only(self):
        opened = []
        app = self.make_app(opener=opened.append)
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("k", "j", "o")
            self.assertEqual(opened, [])
            self.r.hset(f"bus:member:{ROOM}:w1", "workspace", os.path.realpath(os.path.dirname(__file__)))
            await self.load(app, pilot)
            await pilot.press("k", "j", "o")
            self.assertEqual(opened, [os.path.realpath(os.path.dirname(__file__))])

    async def test_sender_without_member_hash_still_appears_in_roster(self):
        self.post(20, "stranger", "all", "hi")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            self.assertEqual(text_of(app.query_one("#roster-label")), "Roster · 4 members, 3 waiting")
            self.assertIn("stranger", text_of(app.query_one("#roster")))


class PureHelperTests(unittest.TestCase):
    @unittest.skipIf(fakeredis is None, "fakeredis/textual not installed")
    def test_plural(self):
        self.assertEqual(board.plural(1, "turn"), "1 turn")
        self.assertEqual(board.plural(0, "turn"), "0 turns")
        self.assertEqual(board.plural(7, "post"), "7 posts")

    @unittest.skipIf(fakeredis is None, "fakeredis/textual not installed")
    def test_short_count_formats(self):
        cases = {0: "0", 999: "999", 1000: "1k", 1234: "1.2k", 12345: "12.3k", 128000: "128k",
                 1234567: "1.2M", 250_000_000: "250M"}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(board.short_count(value), expected)

    @unittest.skipIf(fakeredis is None, "fakeredis/textual not installed")
    def test_fmt_when_today_and_earlier_days(self):
        now = time.mktime((2026, 11, 19, 12, 0, 0, 0, 0, -1))
        today = time.mktime((2026, 11, 19, 9, 5, 0, 0, 0, -1)) * 1000
        earlier = time.mktime((2026, 11, 18, 23, 39, 0, 0, 0, -1)) * 1000
        self.assertEqual(board.fmt_when(today, now), "09:05")
        self.assertEqual(board.fmt_when(earlier, now), "Nov 18 23:39")

    @unittest.skipIf(fakeredis is None, "fakeredis/textual not installed")
    def test_usage_text_omits_missing_parts(self):
        self.assertEqual(board.usage_text(0, 0), "")
        self.assertEqual(board.usage_text(1234, 0), "1.2k tok")
        self.assertEqual(board.usage_text(128000, 0.42), "128k tok · $0.42")

    @unittest.skipIf(fakeredis is None, "fakeredis/textual not installed")
    def test_ago_units(self):
        self.assertEqual(board.ago(0, 45), "45s ago")
        self.assertEqual(board.ago(0, 180), "3m ago")
        self.assertEqual(board.ago(0, 7200), "2h ago")


if __name__ == "__main__":
    unittest.main()
