import json, os, sqlite3, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import search_index_usage as s


def make(tmp):
    os.makedirs(os.path.join(tmp, "agents", "a1", "automations", "r"))
    json.dump({"name": "Alpha"}, open(os.path.join(tmp, "agents", "a1", "profile.json"), "w"))
    json.dump([{"startedAt": 50, "status": "ok"}, {"startedAt": 5, "status": "ok"}],
              open(os.path.join(tmp, "agents", "a1", "automations", "r", "runs.json"), "w"))
    db = os.path.join(tmp, "search-index.db")
    c = sqlite3.connect(db)
    c.execute("create table messages(id integer primary key, agent_id, entry_id, role, timestamp_ms, body, seq)")
    rows = [("a1", "e1", "user", 10, "hello there"), ("a1", "e2", "assistant", 11, "abcd"),
            ("a1", "e3", "assistant", 12, "ef"), ("a1", "e4", "user", 20, "[routine] tick"),
            ("a1", "e5", "assistant", 21, "xyz"), ("a1", "e6", "user", 30, "Cain here. do x"),
            ("b2", "f1", "assistant", 15, "orphan")]
    c.executemany("insert into messages(agent_id,entry_id,role,timestamp_ms,body) values (?,?,?,?,?)", rows)
    c.commit(); c.close()
    st = sqlite3.connect(os.path.join(tmp, "agents", "a1", "store.db"))
    st.execute("create table transcript_entries(seq, id, entry)")
    st.execute("insert into transcript_entries values (1,'e5','{}')"); st.commit(); st.close()
    return db


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); self.db = make(self.tmp)

    def test_classify(self):
        self.assertEqual(s.classify("[routine] x"), "routine")
        self.assertEqual(s.classify("GROK_BOT_HIDDEN_PROMPT hi"), "agent")
        self.assertEqual(s.classify("[agent] hi"), "agent")
        self.assertEqual(s.classify("fix the bug"), "user")

    def test_turns(self):
        t = [x for x in s.load_turns(self.db) if x["agent_id"] == "a1"]
        self.assertEqual([x["kind"] for x in t], ["user", "routine", "agent"])
        self.assertEqual((t[0]["assistant_msgs"], t[0]["assistant_chars"]), (2, 6))

    def test_window_and_dedupe(self):
        t = s.load_turns(self.db, 20, None, {"a1": {"e5"}})
        self.assertEqual(sum(x["assistant_chars"] for x in t), 0)

    def test_readonly(self):
        c = s.open_ro(self.db)
        with self.assertRaises(sqlite3.OperationalError):
            c.execute("delete from messages")

    def test_names_and_routines(self):
        self.assertEqual(s.agent_names(self.tmp)["a1"], "Alpha")
        self.assertEqual(sum(s.routine_runs(self.tmp, 10).values()), 1)
        self.assertEqual(s.transcript_ids(self.tmp, "a1"), {"e5"})

    def test_json_has_no_bodies(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            s.main(["--agent-data", self.tmp, "--json"])
        self.assertNotIn("hello there", buf.getvalue())


if __name__ == "__main__":
    unittest.main()


class T2(unittest.TestCase):
    def test_relayed_phrasing(self):
        for x in ["New task from Dong: build", "Reprioritizing (Dong, 9:06 PM PT): park", "More from Dong (9:30 PM)"]:
            self.assertEqual(s.classify(x), "agent")
        self.assertEqual(s.classify("can you check the Dong portfolio numbers"), "user")
